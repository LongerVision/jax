# Copyright 2021 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utilities for synchronizing and communication across multiple hosts."""

from functools import partial, lru_cache
from typing import Optional
import zlib

from typing import Any
import jax
import jax.numpy as jnp
from jax.tree_util import tree_flatten, tree_map, tree_unflatten
from jax._src import core
from jax._src.interpreters import ad
from jax._src.interpreters import batching
from jax._src.interpreters import mlir
from jax._src import array
from jax._src import sharding_impls
from jax._src.interpreters import pxla
from jax.interpreters import xla
from jax._src import pjit as pjit_lib
from jax.experimental.pjit import pjit
from jax.sharding import PartitionSpec as P
from jax._src import distributed
from jax._src.util import safe_zip
import numpy as np


def _psum(x: Any) -> Any:
  return jax.tree_map(partial(jnp.sum, axis=0), x)


def broadcast_one_to_all(in_tree: Any, is_source: Optional[bool] = None) -> Any:
  """Broadcast data from a source host (host 0 by default) to all other hosts.

  Args:
    in_tree: pytree of arrays - each array *must* have the same shape across the
      hosts.
    is_source: optional bool denoting whether the caller is the source. Only
      'source host' will contribute the data for the broadcast. If None, then
      host 0 is used.

  Returns:
    A pytree matching in_tree where the leaves now all contain the data from the
    first host.
  """
  if is_source is None:
    is_source = jax.process_index() == 0

  devices: np.ndarray = np.array(
      jax.devices()).reshape(jax.process_count(), jax.local_device_count())
  global_mesh = jax.sharding.Mesh(devices, ('processes', 'local_devices'))
  pspec = P('processes')

  def pre_jit(x):
    if is_source:
      inp = x
    else:
      inp = np.zeros_like(x)
    inp = np.expand_dims(inp, axis=0)
    return host_local_array_to_global_array(inp, global_mesh, pspec)

  def post_jit(x):
    return np.asarray(x.addressable_data(0))

  in_tree = jax.tree_map(pre_jit, in_tree)
  out_tree = jax.jit(_psum, out_shardings=jax.sharding.NamedSharding(
      global_mesh, P()))(in_tree)
  return jax.tree_map(post_jit, out_tree)


def sync_global_devices(name: str):
  """Creates a barrier across all hosts/devices."""
  h = np.uint32(zlib.crc32(name.encode()))
  assert_equal(h, f"sync_global_devices name mismatch ('{name}')")


# Identity function is at the top level so that `process_allgather` doesn't
# recompile on every invocation.
def _identity_fn(x):
  return x


def _handle_array_process_allgather(inp, tiled):
  if isinstance(inp, array.ArrayImpl) and not inp.is_fully_addressable:
    reps = sharding_impls.GSPMDSharding.get_replicated(
        inp.sharding._device_assignment)
    out = pjit(_identity_fn, out_shardings=reps)(inp)
  else:
    # All inputs here will be fully addressable.
    if jax.process_count() == 1:
      return np.asarray(inp)

    devices = np.array(jax.devices()).reshape(jax.process_count(),
                                              jax.local_device_count())
    global_mesh = jax.sharding.Mesh(devices, ('processes', 'local_devices'))
    pspec = P('processes')
    s = jax.sharding.NamedSharding(global_mesh, pspec)

    host_np_arr = np.asarray(inp)
    if host_np_arr.ndim == 0 or not tiled:
      host_np_arr = np.expand_dims(host_np_arr, axis=0)

    aval = core.ShapedArray(host_np_arr.shape, host_np_arr.dtype)
    global_aval = pxla.mesh_local_to_global(
        global_mesh, pxla.get_array_mapping(pspec), aval)

    bufs = [jax.device_put(host_np_arr, d) for d in jax.local_devices()]
    global_arr = array.make_array_from_single_device_arrays(
        global_aval.shape, s, bufs)
    with global_mesh:
      out = pjit(_identity_fn, out_shardings=None)(global_arr)

  return np.asarray(out.addressable_data(0))


def process_allgather(in_tree: Any, tiled: bool = False) -> Any:
  """Gather data from across processes.

  Args:
    in_tree: pytree of arrays - each array _must_ have the same shape across the
      hosts.
    tiled: Whether to stack or concat the output. Defaults to False i.e. stack
      into a new positional axis at index 0.
      This does not affect GDA inputs as the GDA output will always be
      concatenated.
      Scalar inputs will always be stacked.

  Returns:
    Pytress of arrays where the data is gathered from all hosts.
      * If the input is a GDA, then the data is fully replicated.
      * If the input is non-GDA, then the output shape is dependent on the
        `tiled` argument. If its False, then the output will be stacked else
        concatenated.
      * If the input is non-GDA and scalar, then the output will be stacked.
  """

  def _pjit(inp):
    return _handle_array_process_allgather(inp, tiled)
  return jax.tree_map(_pjit, in_tree)


def assert_equal(in_tree, fail_message: str = ''):
  """Verifies that all the hosts have the same tree of values."""
  expected = broadcast_one_to_all(in_tree)
  if not jax.tree_util.tree_all(
      jax.tree_util.tree_map(lambda *x: np.all(np.equal(*x)), in_tree, expected)):
    raise AssertionError(
        f'{fail_message} Expected: {expected}; got: {in_tree}.')


def reached_preemption_sync_point(step_id: int) -> bool:
  """Determine whether all hosts have reached a preemption sync step.

  When any host receive a preemption notice, the notice will be propagated to
  all hosts and trigger a synchronization protocol in background. The
  synchronization protocol calculates the maximum step ids from all hosts, and
  uses the next step id (i.e., max + 1) as the safe step to save a checkpoint.
  All hosts should continue training more steps until this method returns True,
  indicating that the `step_id` is equal to the safe step and the hosts should
  start saving a checkpoint. This feature requires enabling
  `jax.config.jax_coordination_service`.

  To use this API, all hosts must start training from the same step and call at
  every training step. Example usage:

  ```
  def should_save(step_id: int) -> bool:

    # Should save an on-demand checkpoint for preemption
    if multihost_utils.reached_preemption_sync_point(step_id):
      return True

    # Should save a regular checkpoint
    return step_id - last_saved_checkpoint_step >= save_interval_steps
  ```

  Preemption notice is provided by the cluster scheduler to notify the
  application in advance before it gets evicted. By default, we use SIGTERM as
  the signal for preemption notice.

  TODO(b/230630494): Add instructions for customized preemption notice.

  Returns:
    A boolean indicating whether all hosts have reached a synchronization step
    after some hosts are preempted.

  Raises:
    RuntimeError: if preemption sync manager has not been inititialized.
  """
  if distributed.global_state.client is None:
    return False
  sync_manager = distributed.global_state.preemption_sync_manager
  if sync_manager is None:
    raise RuntimeError("Preemption sync manager has not been initialized.")
  return sync_manager.reached_sync_point(step_id)


@lru_cache()
def _flatten_pspecs(name, in_tree, pspecs_thunk):
  return pjit_lib.flatten_axis_resources(
      name, in_tree, pspecs_thunk(), tupled_args=True)

@lru_cache()
def _local_to_global_aval(local_aval, mesh, pspec):
  return pxla.mesh_local_to_global(mesh, pxla.get_array_mapping(pspec),
                                   local_aval)

@lru_cache()
def _global_to_local_aval(global_aval, mesh, pspec):
  return pxla.mesh_global_to_local(mesh, pxla.get_array_mapping(pspec),
                                   global_aval)


def host_local_array_to_global_array_impl(
    arr: Any, *, global_mesh: jax.sharding.Mesh, pspec: Any):
  if pspec is None:
    raise ValueError(
        '`None` is not a valid input to the pspecs argument. Please use '
        'jax.sharding.PartitionSpec() if you wanted to replicate your input.')
  # If the Array is not fully addressable i.e. not host local, return it.
  if isinstance(arr, array.ArrayImpl) and not arr.is_fully_addressable:
    return arr
  if isinstance(arr, array.ArrayImpl) and isinstance(
      arr.sharding, jax.sharding.PmapSharding):
    arr = np.array(arr)

  local_sharding = jax.sharding.NamedSharding(global_mesh.local_mesh, pspec)

  # If the input is a concrete jax.Array and the input array sharding
  # matches the `local_sharding`, then there's no need to reshard and create
  # copies.
  if (isinstance(arr, array.ArrayImpl) and
      arr.sharding.is_equivalent_to(local_sharding, arr.ndim)):
    arrays = [x.data for x in arr.addressable_shards]
  else:
    arr = xla.canonicalize_dtype(arr)
    arrays = list(
        arr[index]
        for d, index in local_sharding.devices_indices_map(arr.shape).items())

  global_aval = _local_to_global_aval(
      core.ShapedArray(arr.shape, arr.dtype), global_mesh, pspec)

  return pxla.batched_device_put(
      global_aval, jax.sharding.NamedSharding(global_mesh, pspec),
      arrays, list(global_mesh.local_mesh.devices.flat))


def host_local_array_to_global_array(
    local_inputs: Any, global_mesh: jax.sharding.Mesh, pspecs: Any):
  r"""Converts a host local value to a globally sharded jax.Array.

  You can use this function to transition to jax.Array. Using jax.Array with
  pjit has the same semantics of using GDA with pjit i.e. all jax.Array
  inputs to pjit should be globally shaped.

  If you are currently passing host local values to pjit, you can use this
  function to convert your host local values to global Arrays and then pass that
  to pjit. Example usage.

  >>> from jax.experimental import multihost_utils # doctest: +SKIP
  >>>
  >>> global_inputs = multihost_utils.host_local_array_to_global_array(host_local_inputs, global_mesh, in_pspecs) # doctest: +SKIP
  >>>
  >>> with mesh: # doctest: +SKIP
  >>>   global_out = pjitted_fun(global_inputs) # doctest: +SKIP
  >>>
  >>> host_local_output = multihost_utils.global_array_to_host_local_array(global_out, mesh, out_pspecs) # doctest: +SKIP

  Args:
    local_inputs: A Pytree of host local values.
    global_mesh: A jax.sharding.Mesh object.
    pspecs: A Pytree of jax.sharding.PartitionSpec's.
  """
  flat_inps, in_tree = tree_flatten(local_inputs)
  in_pspecs = _flatten_pspecs('input pspecs', in_tree,
                              pjit_lib.hashable_pytree(pspecs))
  out_flat = [
      host_local_array_to_global_array_p.bind(inp, global_mesh=global_mesh,
                                              pspec=in_spec)
      for inp, in_spec in safe_zip(flat_inps, in_pspecs)
  ]
  return tree_unflatten(in_tree, out_flat)

host_local_array_to_global_array_p = core.Primitive('host_local_array_to_global_array')
host_local_array_to_global_array_p.def_impl(host_local_array_to_global_array_impl)

def ltg_abstract_eval(arr, *, global_mesh, pspec):
  return _local_to_global_aval(
      core.ShapedArray(arr.shape, arr.dtype), global_mesh, pspec)
host_local_array_to_global_array_p.def_abstract_eval(ltg_abstract_eval)

ad.deflinear2(host_local_array_to_global_array_p,
              lambda ct, _, **params: (
                  host_local_array_to_global_array_p.bind(ct, **params),))

def ltg_batcher(insert_axis, spmd_axis_name, axis_size,
                axis_name, main_type, vals_in, dims_in,
                global_mesh, pspec):
  x, = vals_in
  d, = dims_in
  new_parts = None if spmd_axis_name is None else spmd_axis_name
  new_pspec = list(pspec)
  new_pspec.insert(d, new_parts)
  new_pspec = P(*new_pspec)  # type: ignore
  y = host_local_array_to_global_array_p.bind(
      x, global_mesh=global_mesh, pspec=new_pspec)
  return y, d
batching.spmd_axis_primitive_batchers[host_local_array_to_global_array_p] = partial(
    ltg_batcher, False)
batching.axis_primitive_batchers[host_local_array_to_global_array_p] = partial(
    ltg_batcher, False, None)

def _ltg_lowering(ctx, x, *, global_mesh, pspec):
  return [x]
mlir.register_lowering(host_local_array_to_global_array_p, _ltg_lowering)


def global_array_to_host_local_array_impl(
    arr: Any, *, global_mesh: jax.sharding.Mesh, pspec: Any):
  if pspec is None:
    raise ValueError(
        '`None` is not a valid input to the pspecs argument. Please use '
        'jax.sharding.PartitionSpec() if you wanted to replicate your input.')
  # If the Array is already fully addressable i.e. host local, return it.
  if isinstance(arr, array.ArrayImpl) and arr.is_fully_addressable:
    return arr

  global_sharding = jax.sharding.NamedSharding(global_mesh, pspec)
  local_sharding = jax.sharding.NamedSharding(global_mesh.local_mesh, pspec)
  local_aval = _global_to_local_aval(
      core.ShapedArray(arr.shape, arr.dtype), global_mesh, pspec)

  if isinstance(arr, array.ArrayImpl):
    if arr.sharding.is_equivalent_to(global_sharding, arr.ndim):
      arrays = arr._arrays
    else:
      resharded_array = jax.device_put(arr, global_sharding)
      arrays = resharded_array._arrays
    return array.ArrayImpl(local_aval, local_sharding, arrays, committed=True)
  else:
    # numpy array can show up here during AD.
    arr = xla.canonicalize_dtype(arr)
    arrays = list(
        arr[index]
        for d, index in local_sharding.devices_indices_map(arr.shape).items())
    return pxla.batched_device_put(
        local_aval, local_sharding, arrays,
        list(global_mesh.local_mesh.devices.flat))


def global_array_to_host_local_array(
    global_inputs: Any, global_mesh: jax.sharding.Mesh, pspecs: Any):
  r"""Converts a global `jax.Array` to a host local `jax.Array`.

  You can use this function to transition to `jax.Array`. Using `jax.Array` with
  pjit has the same semantics of using GDA with pjit i.e. all `jax.Array`
  inputs to pjit should be globally shaped and the output from pjit will also
  be globally shaped jax.Array's

  You can use this function to convert the globally shaped `jax.Array` output
  from pjit to host local values again so that the transition to jax.Array can
  be a mechanical change. Example usage

  >> from jax.experimental import multihost_utils # doctest: +SKIP
  >>
  >> global_inputs = multihost_utils.host_local_array_to_global_array(host_local_inputs, global_mesh, in_pspecs) # doctest: +SKIP
  >>
  >> with mesh: # doctest: +SKIP
  >>   global_out = pjitted_fun(global_inputs) # doctest: +SKIP
  >>
  >> host_local_output = multihost_utils.global_array_to_host_local_array(global_out, mesh, out_pspecs) # doctest: +SKIP

  Args:
    global_inputs: A Pytree of global jax.Array's.
    global_mesh: A jax.sharding.Mesh object.
    pspecs: A Pytree of jax.sharding.PartitionSpec's.
  """
  flat_inps, out_tree = tree_flatten(global_inputs)
  out_pspecs = _flatten_pspecs('output pspecs', out_tree,
                               pjit_lib.hashable_pytree(pspecs))
  out_flat = [
      global_array_to_host_local_array_p.bind(inp, global_mesh=global_mesh,
                                              pspec=o)
      for inp, o in safe_zip(flat_inps, out_pspecs)
  ]
  return tree_unflatten(out_tree, out_flat)

global_array_to_host_local_array_p = core.Primitive('global_array_to_host_local_array')
global_array_to_host_local_array_p.def_impl(global_array_to_host_local_array_impl)

def gtl_abstract_eval(arr, *, global_mesh, pspec):
  return _global_to_local_aval(
      core.ShapedArray(arr.shape, arr.dtype), global_mesh, pspec)
global_array_to_host_local_array_p.def_abstract_eval(gtl_abstract_eval)

ad.deflinear2(global_array_to_host_local_array_p,
              lambda ct, _, **params: (
                  global_array_to_host_local_array_p.bind(ct, **params),))
batching.defvectorized(global_array_to_host_local_array_p)

def _gtl_lowering(ctx, x, *, global_mesh, pspec):
  return [x]
mlir.register_lowering(global_array_to_host_local_array_p, _gtl_lowering)
