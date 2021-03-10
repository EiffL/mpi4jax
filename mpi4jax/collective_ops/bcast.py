import numpy as _np
from mpi4py import MPI as _MPI

from jax import abstract_arrays, core
from jax.core import Primitive
from jax.interpreters import xla  # , ad
from jax.lib import xla_client

from jax.lax import create_token
from ..utils import (
    HashableMPIType,
    _constant_s32_scalar,
    _constant_u64_scalar,
    _ops,
    _unpack_builder,
    default_primitive_impl,
    dtype_ptr,
    to_mpi_ptr,
    unpack_hashable,
    wrap_as_hashable,
)
from ..validation import enforce_types
from ..warn import warn_missing_omnistaging

# The Jax primitive
mpi_bcast_p = Primitive("bcast_mpi")  # Create the primitive
mpi_bcast_impl = default_primitive_impl(mpi_bcast_p)


# This function applies the primitive to an AST
@enforce_types(
    root=(_np.integer),
    comm=(_MPI.Intracomm, HashableMPIType),
    token=(type(None), xla.Token, core.Tracer),
)
def Bcast(x, root, comm=_MPI.COMM_WORLD, token=None):
    """Perform a Bcast (broadcast) operation.

    .. warning::

        Unlike mpi4py's Bcast, this returns a *new* array with the received data.

    Arguments:
        x: Array or scalar input. Data is only read on root process. On non-root
           processes, this is used to determine the shape and dtype of the result.
        root (int): The process to use as source.
        comm (mpi4py.MPI.Comm): The MPI communicator to use (defaults to
            :obj:`COMM_WORLD`).
        token: XLA token to use to ensure correct execution order. If not given,
            a new token is generated.

    Returns:
        Tuple[DeviceArray, Token]:
            - Received data.
            - A new, modified token, that depends on this operation.

    """
    if token is None:
        token = create_token(x)

    comm = wrap_as_hashable(comm)
    return mpi_bcast_p.bind(x, token, root=root, comm=comm)


#  This function compiles the operation
def mpi_bcast_xla_encode_cpu(c, x, token, root, comm):
    warn_missing_omnistaging()

    comm = unpack_hashable(comm)

    c = _unpack_builder(c)
    x_shape = c.GetShape(x)
    dtype = x_shape.element_type()
    dims = x_shape.dimensions()

    # compute total number of elements in array
    _nitems = _constant_s32_scalar(c, _np.prod(dims, dtype=int))
    _dtype_ptr = dtype_ptr(dtype)

    sh = xla_client.Shape.tuple_shape(
        [xla_client.Shape.array_shape(dtype, dims), xla_client.Shape.token_shape()]
    )

    return _ops.CustomCall(
        c,
        b"mpi_bcast",
        operands=(
            _nitems,
            x,
            _constant_s32_scalar(c, root),
            _constant_u64_scalar(c, to_mpi_ptr(comm)),
            _constant_u64_scalar(c, _dtype_ptr),
            token,
        ),
        shape=sh,
        has_side_effect=True,
    )


def mpi_bcast_xla_encode_gpu(c, x, token, root, comm):
    from ..cython.mpi_xla_bridge_gpu import build_bcast_descriptor

    warn_missing_omnistaging()

    comm = unpack_hashable(comm)

    c = _unpack_builder(c)
    x_shape = c.GetShape(x)
    dtype = x_shape.element_type()
    dims = x_shape.dimensions()

    # compute total number of elements in array
    _nitems = _np.prod(dims, dtype=int)
    _dtype_ptr = dtype_ptr(dtype)

    sh = xla_client.Shape.tuple_shape(
        [xla_client.Shape.array_shape(dtype, dims), xla_client.Shape.token_shape()]
    )

    descriptor = build_bcast_descriptor(
        _nitems,
        root,
        to_mpi_ptr(comm),
        _dtype_ptr,
    )

    return _ops.CustomCall(
        c,
        b"mpi_bcast",
        operands=(
            x,
            token,
        ),
        shape=sh,
        opaque=descriptor,
        has_side_effect=True,
    )


# This function evaluates only the shapes during AST construction
def mpi_bcast_abstract_eval(xs, token, root, comm):
    return (
        abstract_arrays.ShapedArray(xs.shape, xs.dtype),
        abstract_arrays.abstract_token,
    )


# def mpi_bcast_value_and_jvp(in_args, tan_args, root, comm):
#    x, token = in_args
#    x_tan, token_tan = tan_args
#
#    res = Bcast(x, token=token, dest=dest, comm=comm)
#
#    if comm.rank == root:
#        jvp = (x_tan, token_tan)
#    else:
#        jvp = (None, token_tan)
#
#    return (res, jvp)


mpi_bcast_p.multiple_results = True
mpi_bcast_p.def_impl(mpi_bcast_impl)
mpi_bcast_p.def_abstract_eval(mpi_bcast_abstract_eval)

# ad.primitive_jvps[mpi_bcast_p] = mpi_bcast_value_and_jvp

# assign to the primitive the correct encoder
xla.backend_specific_translations["cpu"][mpi_bcast_p] = mpi_bcast_xla_encode_cpu
xla.backend_specific_translations["gpu"][mpi_bcast_p] = mpi_bcast_xla_encode_gpu