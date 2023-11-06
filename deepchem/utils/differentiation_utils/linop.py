from __future__ import annotations
from typing import Sequence, Optional, List
import warnings
import traceback
import torch
from abc import abstractmethod
from contextlib import contextmanager
from scipy.sparse.linalg import LinearOperator as spLinearOperator
from deepchem.utils.differentiation_utils import EditableModule
from deepchem.utils.differentiation_utils import get_bcasted_dims

__all__ = ["LinearOperator"]


class LinearOperator(EditableModule):
    """
    ``LinearOperator`` is a base class designed to behave as a linear operator
    without explicitly determining the matrix. This ``LinearOperator`` should
    be able to operate as batched linear operators where its shape is
    ``(B1,B2,...,Bb,p,q)`` with ``B*`` as the (optional) batch dimensions.
    For a user-defined class to behave as ``LinearOperator``, it must use
    ``LinearOperator`` as one of the parent and it has to have ``._mv()``
    method implemented and ``._getparamnames()`` if used in xitorch's
    functionals with torch grad enabled.

    Examples
    --------
    >>> import torch
    >>> seed = torch.manual_seed(100)
    >>> class MyLinOp(LinearOperator):
    ...     def __init__(self, shape):
    ...         super(MyLinOp, self).__init__(shape)
    ...         self.param = torch.rand(shape)
    ...     def _getparamnames(self, prefix=""):
    ...         return [prefix + "param"]
    ...     def _mv(self, x):
    ...         return torch.matmul(self.param, x)
    ...     def _rmv(self, x):
    ...         return torch.matmul(self.param.transpose(-2,-1).conj(), x)
    ...     def _mm(self, x):
    ...         return torch.matmul(self.param, x)
    ...     def _rmm(self, x):
    ...         return torch.matmul(self.param.transpose(-2,-1).conj(), x)
    ...     def _fullmatrix(self):
    ...         return self.param
    >>> linop = MyLinOp((1,3,1,2))
    >>> print(linop)
    LinearOperator (MyLinOp) with shape (1, 3, 1, 2), dtype = torch.float32, device = cpu
    >>> x = torch.rand(1,3,2,2)
    >>> linop.mv(x)
    tensor([[[[0.1991, 0.1011]],
    <BLANKLINE>
             [[0.3764, 0.5742]],
    <BLANKLINE>
             [[1.0345, 1.1802]]]])
    >>> x = torch.rand(1,3,1,1)
    >>> linop.rmv(x)
    tensor([[[[0.0250],
              [0.1827]],
    <BLANKLINE>
             [[0.0794],
              [0.1463]],
    <BLANKLINE>
             [[0.1207],
              [0.1345]]]])
    >>> x = torch.rand(1,3,2,2)
    >>> linop.mm(x)
    tensor([[[[0.8891, 0.4243]],
    <BLANKLINE>
             [[0.4856, 0.3128]],
    <BLANKLINE>
             [[0.6601, 0.9532]]]])
    >>> x = torch.rand(1,3,1,2)
    >>> linop.rmm(x)
    tensor([[[[0.0473, 0.0019],
              [0.3455, 0.0138]],
    <BLANKLINE>
             [[0.0580, 0.2504],
              [0.1069, 0.4614]],
    <BLANKLINE>
             [[0.4779, 0.1102],
              [0.5326, 0.1228]]]])
    >>> linop.fullmatrix()
    tensor([[[[0.1117, 0.8158]],
    <BLANKLINE>
             [[0.2626, 0.4839]],
    <BLANKLINE>
             [[0.6765, 0.7539]]]])

    """
    _implementation_checked = False
    _is_mv_implemented = False
    _is_mm_implemented = False
    _is_rmv_implemented = False
    _is_rmm_implemented = False
    _is_fullmatrix_implemented = False
    _is_gpn_implemented = False

    def __new__(self, *args, **kwargs):
        """Check the implemented functions in the class."""
        if not self._implementation_checked:
            self._is_mv_implemented = self._check_if_implemented("_mv")
            self._is_mm_implemented = self._check_if_implemented("_mm")
            self._is_rmv_implemented = self._check_if_implemented("_rmv")
            self._is_rmm_implemented = self._check_if_implemented("_rmm")
            self._is_fullmatrix_implemented = self._check_if_implemented(
                "_fullmatrix")
            self._is_gpn_implemented = self._check_if_implemented(
                "_getparamnames")

            self._implementation_checked = True

            if not self._is_mv_implemented:
                raise RuntimeError(
                    "LinearOperator must have at least _mv(self) "
                    "method implemented")
        return super(LinearOperator, self).__new__(self)

    @classmethod
    def _check_if_implemented(self, methodname: str) -> bool:
        """Check if the method is implemented in the class.

        Parameters
        ----------
        methodname : str
            The method name to be checked

        """
        this_method = getattr(self, methodname)
        base_method = getattr(LinearOperator, methodname)
        return this_method is not base_method

    def __init__(self,
                 shape: Sequence[int],
                 is_hermitian: bool = False,
                 dtype: Optional[torch.dtype] = None,
                 device: Optional[torch.device] = None,
                 _suppress_hermit_warning: bool = False) -> None:
        """Initialize the ``LinearOperator``."""

        super(LinearOperator, self).__init__()
        if len(shape) < 2:
            raise RuntimeError("The shape must have at least 2 dimensions")
        self._shape = shape
        self._batchshape = list(shape[:-2])
        self._is_hermitian = is_hermitian
        self._dtype = dtype if dtype is not None else torch.float32
        self._device = device if device is not None else torch.device("cpu")
        if is_hermitian and shape[-1] != shape[-2]:
            raise RuntimeError(
                "The object is indicated as Hermitian, but the shape is not square"
            )

        # check which methods are implemented
        if not _suppress_hermit_warning and self._is_hermitian and \
           (self._is_rmv_implemented or self._is_rmm_implemented):
            warnings.warn(
                "The LinearOperator is Hermitian with implemented "
                "rmv or rmm. We will use the mv and mm methods "
                "instead",
                stacklevel=2)

    def __repr__(self) -> str:
        """Representation of the ``LinearOperator``.

        Returns
        -------
        shape: Sequence[int]
            The shape of the linear operator.
        dtype: torch.dtype
            The dtype of the linear operator.
        device: torch.device
            The device of the linear operator.

        """
        return "LinearOperator (%s) with shape %s, dtype = %s, device = %s" % \
            (self.__class__.__name__, _shape2str(self.shape), self.dtype, self.device)

    @abstractmethod
    def _getparamnames(self, prefix: str = "") -> List[str]:
        """
        List the self's parameters that affecting the ``LinearOperator``.
        This is for the derivative purpose.

        Parameters
        ----------
        prefix: str
            The prefix to be appended in front of the parameters name.
            This usually contains the dots.

        Returns
        -------
        List[str]
            List of parameter names (including the prefix) that affecting
            the ``LinearOperator``.

        """
        return []

    @abstractmethod
    def _mv(self, x: torch.Tensor) -> torch.Tensor:
        """
        Abstract method to be implemented for matrix-vector multiplication.
        Required for all ``LinearOperator`` objects.

        Parameters
        ----------
        x: torch.tensor
            Vector with shape ``(...,q)`` where the linear operation is operated on.

        Returns
        -------
        torch.tensor
            The result of the linear operation with shape ``(...,p)``

        """
        pass

    # @abstractmethod
    def _rmv(self, x: torch.Tensor) -> torch.Tensor:
        """
        Abstract method to be implemented for transposed matrix-vector
        multiplication. Optional. If not implemented, it will use the
        adjoint trick to compute ``.rmv()``. Usually implemented for
        efficiency reasons.

        Parameters
        ----------
        x: torch.tensor
            Vector with shape ``(...,q)`` where the linear operation is operated on.

        Returns
        -------
        torch.tensor
            The result of the linear operation with shape ``(...,p)``

        """
        raise NotImplementedError()

    # @abstractmethod # (optional)
    def _mm(self, x: torch.Tensor) -> torch.Tensor:
        """
        Abstract method to be implemented for matrix-matrix multiplication.
        If not implemented, then it uses batched version of matrix-vector
        multiplication. Usually this is implemented for efficiency reasons.

        Parameters
        ----------
        x: torch.tensor
            Vector with shape ``(...,q)`` where the linear operation is operated on.

        Returns
        -------
        torch.tensor
            The result of the linear operation with shape ``(...,p)``

        """
        raise NotImplementedError()

    # @abstractmethod
    def _rmm(self, x: torch.Tensor) -> torch.Tensor:
        """
        Abstract method to be implemented for transposed matrix-matrix
        multiplication. If not implemented, then it uses batched version
        of transposed matrix-vector multiplication. Usually this is
        implemented for efficiency reasons.

        Parameters
        ----------
        x: torch.tensor
            Vector with shape ``(...,q)`` where the linear operation is operated on.

        Returns
        -------
        torch.tensor
            The result of the linear operation with shape ``(...,p)``

        """
        raise NotImplementedError()

    # @abstractmethod
    def _fullmatrix(self) -> torch.Tensor:
        raise NotImplementedError()

    # linear operators must have a set of parameters that affects most of
    # the methods (i.e. mm, mv, rmm, rmv)
    def getlinopparams(self) -> Sequence[torch.Tensor]:
        return self.getuniqueparams("mm")

    @contextmanager
    def uselinopparams(self, *params):
        methodname = "mm"
        try:
            _orig_params_ = self.getuniqueparams(methodname)
            self.setuniqueparams(methodname, *params)
            yield self
        finally:
            self.setuniqueparams(methodname, *_orig_params_)

    # implemented functions
    def mv(self, x: torch.Tensor) -> torch.Tensor:
        """
        Apply the matrix-vector operation to vector ``x`` with shape ``(...,q)``.
        The batch dimensions of ``x`` need not be the same as the batch dimensions
        of the ``LinearOperator``, but it must be broadcastable.

        Parameters
        ----------
        x: torch.tensor
            The vector with shape ``(...,q)`` where the linear operation is operated on

        Returns
        -------
        y: torch.tensor
            The result of the linear operation with shape ``(...,p)``

        """
        self._assert_if_init_executed()
        if x.shape[-1] != self.shape[-1]:
            raise RuntimeError(
                "Cannot operate .mv on shape %s. Expected (...,%d)" %
                (str(tuple(x.shape)), self.shape[-1]))

        return self._mv(x)

    def mm(self, x: torch.Tensor) -> torch.Tensor:
        """
        Apply the matrix-matrix operation to matrix ``x`` with shape ``(...,q,r)``.
        The batch dimensions of ``x`` need not be the same as the batch dimensions
        of the ``LinearOperator``, but it must be broadcastable.

        Parameters
        ----------
        x: torch.tensor
            The matrix with shape ``(...,q,r)`` where the linear operation is
            operated on.

        Returns
        -------
        y: torch.tensor
            The result of the linear operation with shape ``(...,p,r)``

        """
        self._assert_if_init_executed()
        if x.shape[-2] != self.shape[-1]:
            raise RuntimeError(
                "Cannot operate .mm on shape %s. Expected (...,%d,*)" %
                (str(tuple(x.shape)), self.shape[-1]))

        xbatchshape = list(x.shape[:-2])
        if self._is_mm_implemented:
            return self._mm(x)
        else:
            # use batched mv as mm

            # move the last dimension to the very first dimension to be broadcasted
            if len(xbatchshape) < len(self._batchshape):
                xbatchshape = [1] * (len(self._batchshape) -
                                     len(xbatchshape)) + xbatchshape
            x1 = x.reshape(1, *xbatchshape, *x.shape[-2:])
            xnew = x1.transpose(0, -1).squeeze(-1)  # (r,...,q)

            # apply batched mv and restore the initial shape
            ynew = self._mv(xnew)  # (r,...,p)
            y = ynew.unsqueeze(-1).transpose(0, -1).squeeze(0)  # (...,p,r)
            return y

    def rmv(self, x: torch.Tensor) -> torch.Tensor:
        """
        Apply the matrix-vector adjoint operation to vector ``x`` with shape ``(...,p)``,
        i.e. ``A^H x``.
        The batch dimensions of ``x`` need not be the same as the batch dimensions
        of the ``LinearOperator``, but it must be broadcastable.

        Parameters
        ----------
        x: torch.tensor
            The vector of shape ``(...,p)`` where the adjoint linear operation is operated at.

        Returns
        -------
        y: torch.tensor
            The result of the adjoint linear operation with shape ``(...,q)``

        """
        self._assert_if_init_executed()
        if x.shape[-1] != self.shape[-2]:
            raise RuntimeError(
                "Cannot operate .rmv on shape %s. Expected (...,%d)" %
                (str(tuple(x.shape)), self.shape[-2]))

        if self._is_hermitian:
            return self._mv(x)
        elif not self._is_rmv_implemented:
            return self._adjoint_rmv(x)
        return self._rmv(x)

    def rmm(self, x: torch.Tensor) -> torch.Tensor:
        """
        Apply the matrix-matrix adjoint operation to matrix ``x`` with shape ``(...,p,r)``,
        i.e. ``A^H X``.
        The batch dimensions of ``x`` need not be the same as the batch dimensions
        of the ``LinearOperator``, but it must be broadcastable.

        Parameters
        ----------
        x: torch.Tensor
            The matrix of shape ``(...,p,r)`` where the adjoint linear operation is operated on.

        Returns
        -------
        y: torch.Tensor
            The result of the adjoint linear operation with shape ``(...,q,r)``.

        """
        self._assert_if_init_executed()
        if x.shape[-2] != self.shape[-2]:
            raise RuntimeError(
                "Cannot operate .rmm on shape %s. Expected (...,%d,*)" %
                (str(tuple(x.shape)), self.shape[-2]))

        if self._is_hermitian:
            return self.mm(x)

        xbatchshape = list(x.shape[:-2])
        if self._is_rmm_implemented:
            return self._rmm(x)
        else:
            # use batched mv as mm
            rmv = self._rmv if self._is_rmv_implemented else self.rmv

            # move the last dimension to the very first dimension to be broadcasted
            if len(xbatchshape) < len(self._batchshape):
                xbatchshape = [1] * (len(self._batchshape) -
                                     len(xbatchshape)) + xbatchshape
            x1 = x.reshape(1, *xbatchshape, *x.shape[-2:])  # (1,...,p,r)
            xnew = x1.transpose(0, -1).squeeze(-1)  # (r,...,p)

            # apply batched mv and restore the initial shape
            ynew = rmv(xnew)  # (r,...,q)
            y = ynew.unsqueeze(-1).transpose(0, -1).squeeze(0)  # (...,q,r)
            return y

    def fullmatrix(self) -> torch.Tensor:
        """Full matrix representation of the linear operator."""
        if self._is_fullmatrix_implemented:
            return self._fullmatrix()
        else:
            self._assert_if_init_executed()
            nq = self._shape[-1]
            V = torch.eye(nq, dtype=self._dtype, device=self._device)  # (nq,nq)
            return self.mm(V)  # (B1,B2,...,Bb,np,nq)

    def scipy_linalg_op(self):
        """Return the scipy.sparse.linalg.LinearOperator object of the linear operator."""

        def to_tensor(x):
            return torch.tensor(x, dtype=self.dtype, device=self.device)

        return spLinearOperator(
            shape=self.shape,
            matvec=lambda v: self.mv(to_tensor(v)).detach().cpu().numpy(),
            rmatvec=lambda v: self.rmv(to_tensor(v)).detach().cpu().numpy(),
            matmat=lambda v: self.mm(to_tensor(v)).detach().cpu().numpy(),
            rmatmat=lambda v: self.rmm(to_tensor(v)).detach().cpu().numpy(),
        )

    def getparamnames(self, methodname: str, prefix: str = "") -> List[str]:
        """Get the parameter names that affects the method."""

        if methodname in ["mv", "rmv", "mm", "rmm", "fullmatrix"]:
            return self._getparamnames(prefix=prefix)
        else:
            raise KeyError("getparamnames for method %s is not implemented" %
                           methodname)

    def __rsub__(self, b):
        return b.__sub__(self)

    # properties
    @property
    def dtype(self) -> torch.dtype:
        """The dtype of the linear operator."""
        return self._dtype

    @property
    def device(self) -> torch.device:
        """The device of the linear operator."""
        return self._device

    @property
    def shape(self) -> Sequence[int]:
        """The shape of the linear operator."""
        return self._shape

    @property
    def is_hermitian(self) -> bool:
        """Whether the linear operator is Hermitian."""
        return self._is_hermitian

    # implementation
    @property
    def is_mv_implemented(self) -> bool:
        """Whether the ``.mv()`` method is implemented."""
        return True

    @property
    def is_mm_implemented(self) -> bool:
        """Whether the ``.mm()`` method is implemented."""
        return self._is_mm_implemented

    @property
    def is_rmv_implemented(self) -> bool:
        """Whether the ``.rmv()`` method is implemented."""
        return self._is_rmv_implemented

    @property
    def is_rmm_implemented(self) -> bool:
        """Whether the ``.rmm()`` method is implemented."""
        return self._is_rmm_implemented

    @property
    def is_fullmatrix_implemented(self) -> bool:
        """Whether the ``.fullmatrix()`` method is implemented."""
        return self._is_fullmatrix_implemented

    @property
    def is_getparamnames_implemented(self) -> bool:
        """Whether the ``._getparamnames()`` method is implemented."""
        return self._is_gpn_implemented

    # debug functions
    def check(self, warn: Optional[bool] = None) -> None:
        """
        Perform checks to make sure the ``LinearOperator`` behaves as a proper
        linear operator.

        Parameters
        ----------
        warn: bool or None
            If ``True``, then raises a warning to the user that the check might slow
            down the program. This is to remind the user to turn off the check
            when not in a debugging mode.
            If ``None``, it will raise a warning if it runs not in a debug mode, but
            will be silent if it runs in a debug mode.

        Raises
        ------
        RuntimeError
            Raised if an error is raised when performing linear operations of the
            object (e.g. calling ``.mv()``, ``.mm()``, etc)
        AssertionError
            Raised if the linear operations do not behave as proper linear operations.
            (e.g. not scaling linearly)

        """
        # if warn is None:
        #     warn = not is_debug_enabled()
        # TODO: To verify if omiting this causes error.
        if warn:
            msg = "The linear operator check is performed. This might slow down your program."
            warnings.warn(msg, stacklevel=2)
        checklinop(self)
        print("Check linear operator done")

    # private functions
    def _adjoint_rmv(self, xt: torch.Tensor) -> torch.Tensor:
        """calculate the right matvec multiplication by using the adjoint trick.

        Parameters
        ----------
        xt: torch.tensor
            The vector of shape ``(...,p)`` where the adjoint linear operation is operated at.

        Returns
        -------
        torch.tensor
            The result of the adjoint linear operation with shape ``(...,q)``

        """
        # xt: (*BY, p)
        # xdummy: (*BY, q)

        BY = xt.shape[:-1]
        BA = self.shape[:-2]
        BAY = get_bcasted_dims(BY, BA)

        # calculate y = Ax
        p, q = self.shape[-2:]
        xdummy = torch.zeros((*BAY, q), dtype=xt.dtype,
                             device=xt.device).requires_grad_()
        with torch.enable_grad():
            y = self.mv(xdummy)  # (*BAY, p)

        # calculate (dL/dx)^T = A^T (dL/dy)^T with (dL/dy)^T = xt
        xt2 = xt.contiguous().expand_as(y)  # (*BAY, p)
        res = torch.autograd.grad(
            y, xdummy, grad_outputs=xt2,
            create_graph=torch.is_grad_enabled())[0]  # (*BAY, q)
        return res

    # def __check_if_implemented(self, methodname: str) -> bool:
    #     this_method = getattr(self, methodname).__func__
    #     base_method = getattr(LinearOperator, methodname)
    #     return this_method is not base_method

    def _assert_if_init_executed(self):
        if not hasattr(self, "_shape"):
            raise RuntimeError("super().__init__ must be executed first")


def checklinop(linop: LinearOperator) -> None:
    """
    Check if the implemented mv and mm can receive the possible shapes and returns
    the correct shape. If an error is found, then this function raise AssertionError.

    Parameters
    ----------
    linop: LinearOperator
        The instance of LinearOperator to be checked

    Exception
    ---------
    AssertionError
        Raised if there is a shape mismatch
    RuntimeError
        Raised if there is an error when evaluating the .mv, .mm, .rmv, or .rmm methods

    """
    shape = linop.shape
    p, q = shape[-2:]
    batchshape = shape[:-2]

    def runtest(methodname, xshape, yshape):
        x = torch.rand(xshape, dtype=linop.dtype, device=linop.device)
        fcn = getattr(linop, methodname)
        try:
            y = fcn(x)
        except Exception:
            s = traceback.format_exc()
            msg = "An error is raised from .%s with input shape: %s (linear operator shape: %s)\n" % \
                (methodname, tuple(xshape), tuple(linop.shape))
            msg += "--- full traceback ---\n%s" % s
            raise RuntimeError(msg)
        msg = "The output shape of .%s is not correct. Input: %s, expected output: %s, output: %s" % \
            (methodname, tuple(x.shape), tuple(yshape), tuple(y.shape))
        msg += "\n" + str(linop)
        assert list(y.shape) == list(yshape), msg

        # linearity test
        x2 = 1.25 * x
        y2 = fcn(x2)
        msg = "Linearity check fails\n%s\n" % str(linop)
        assert torch.allclose(y2, 1.25 * y), msg
        y0 = fcn(0 * x)
        assert torch.allclose(
            y0, y * 0), "Linearity check (with 0) fails\n" + str(linop)

        # batched test
        xnew = torch.cat((x.unsqueeze(0), x2.unsqueeze(0)), dim=0)
        ynew = fcn(xnew)  # (2, ..., q)
        msg = "Batched test fails (expanding batches changes the results)" + str(
            linop)
        assert torch.allclose(ynew[0], y), msg
        assert torch.allclose(ynew[1], y2), msg

    # generate shapes
    mv_xshapes = [
        (q,),
        (1, q),
        (1, 1, q),
        (*batchshape, q),
        (1, *batchshape, q),
    ]
    mv_yshapes = [(*batchshape, p),
                  (*batchshape, p) if len(batchshape) >= 1 else (1, p),
                  (*batchshape, p) if len(batchshape) >= 2 else (1, 1, p),
                  (*batchshape, p), (1, *batchshape, p)]
    # test matvec and matmat, run input in multiple shapes to make sure no error is raised
    r = 2
    for (mv_xshape, mv_yshape) in zip(mv_xshapes, mv_yshapes):
        runtest("mv", mv_xshape, mv_yshape)
        runtest("mm", (*mv_xshape, r), (*mv_yshape, r))

    if not linop.is_rmv_implemented:
        return

    rmv_xshapes = [
        (p,),
        (1, p),
        (1, 1, p),
        (*batchshape, p),
        (1, *batchshape, p),
    ]
    rmv_yshapes = [(*batchshape, q),
                   (*batchshape, q) if len(batchshape) >= 1 else (1, q),
                   (*batchshape, q) if len(batchshape) >= 2 else (1, 1, q),
                   (*batchshape, q), (1, *batchshape, q)]
    for (rmv_xshape, rmv_yshape) in zip(rmv_xshapes, rmv_yshapes):
        runtest("rmv", rmv_xshape, rmv_yshape)
        runtest("rmm", (*rmv_xshape, r), (*rmv_yshape, r))


def _shape2str(shape):
    return "(%s)" % (", ".join([str(s) for s in shape]))
