from typing import overload, Union, Optional, Callable
import logging
import weakref

import numpy as np

import sunode
from sunode.basic import CPointer, ERRORS, lib, ffi, check, check_ptr, Borrows
from sunode.problem import Problem
from sunode import matrix, vector


logger = logging.getLogger('sunode.solver')


class SolverError(RuntimeError):
    pass


class BaseSolver(Borrows):
    problem: Problem
    user_data: np.ndarray

    def __init__(self, problem: Problem, *, solver: str = 'BDF', jac_kind: str = "dense"):
        super().__init__()

        self.problem = problem
        self.user_data = problem.make_user_data()

        self._state_buffer = sunode.empty_vector(self.n_states)
        self._state_buffer.data[:] = 0.

        self.borrow(self._state_buffer)

        if jac_kind == 'dense':
            self._jac = matrix.empty_matrix((self.n_states, self.n_states))
        elif jac_kind == 'sparse':
            self._jac = problem.make_sparse_jac_template()
        else:
            raise ValueErorr(f'Unknown jac_kind {jac_kind}.')

        self.borrow(self._jac)

        if solver == 'BDF':
            self.c_ptr = check_ptr(lib.CVodeCreate(lib.CV_BDF))
        elif solver == 'ADAMS':
            self.c_ptr = check_ptr(lib.CVodeCreate(lib.CV_ADAMS))
        else:
            raise ValueError(f'Unknown solver {solver}.')

        self._rhs = self.problem.make_sundials_rhs()

        def finalize(c_ptr: CPointer, release_borrowed: Callable[[], None]) -> None:
            if c_ptr == ffi.NULL:
                logger.warn("Trying to free Solver, but it is NULL.")
            else:
                logger.debug("Freeing Solver")
                lib.CVodeFree(c_ptr)
            release_borrowed()
        weakref.finalize(self, finalize, self.c_ptr, self.release_borrowed_func())

    def init(self, t0, state: Optional[np.ndarray] = None, recreate_rhs: bool = False):
        if state is not None:
            self.state[:] = state
        if recreate_rhs:
            self._rhs = self.problem.make_sundials_rhs()
        check(lib.CVodeInit(self.c_ptr, self._rhs.cffi, t0, self._state_buffer.c_ptr))

    def set_tolerance(self, rtol: float, atol: Union[np.ndarray, float]) -> None:
        self._atol = np.array(atol)
        self._rtol = rtol

        if atol.ndim == 1:
            if not hasattr(self, '_atol_buffer'):
                self._atol_buffer = sunode.from_numpy(atol)
                self.borrow(self._atol_buffer)
            atol_buffer.data[:] = atol
            check(lib.CVodeSVtolerances(self.c_ptr, self._rtol, self._atol_buffer.c_ptr))
        elif atol.ndim == 0:
            check(lib.CVodeSStolerances(self.c_ptr, rtol, atol))
        else:
            raise ValueError('Invalid absolute tolerances.')

    def set_constraints(self, constraints: Optional[np.ndarray]) -> None:
        if constraints is None:
            check(lib.CVodeSetConstraints(self.c_ptr, ffi.NULL))
            return

        assert constraints.shape == (self.n_states,)
        if not hasattr(self, '_constraints_buffer'):
            self._constraints_buffer = sunode.from_numpy(constraints)
            self.borrow(self._constraints_buffer)
        self._constraints_buffer.data[:] = constraints
        check(lib.CVodeSetConstraints(self.c_ptr, self._constraints_buffer.c_ptr))

    def make_output_buffers(self, tvals: np.ndarray):
        n_states = self._problem.n_states
        n_params = self._problem.n_params
        y_vals = np.zeros((len(tvals), n_states))
        if self._compute_sens:
            sens_vals = np.zeros((len(tvals), n_params, n_states))
            return y_vals, sens_vals
        return y_vals

    def as_xarray(self, tvals, out, sens_out=None, unstack_state=True, unstack_params=True):
        return self._problem.solution_to_xarray(
            tvals, out, self._user_data,
            sensitivity=sens_out,
            unstack_state=unstack_state, unstack_params=unstack_params
        )

    def solve(self, t0, tvals, y0, y_out, forward_sens=None, checkpointing=False):
        CVodeReInit = lib.CVodeReInit
        CVodeAdjReInit = lib.CVodeAdjReInit
        CVodeF = lib.CVodeF
        ode = self._ode
        TOO_MUCH_WORK = lib.CV_TOO_MUCH_WORK

        state_data = self._state_buffer.data
        state_c_ptr = self._state_buffer.c_ptr

        state_data[:] = y0

        time_p = ffi.new('double*')
        time_p[0] = t0

        n_check = ffi.new('int*')
        n_check[0] = 0

        check(CVodeReInit(ode, t0, state_c_ptr))
        check(CVodeAdjReInit(ode))

        for i, t in enumerate(tvals):
            if t == t0:
                y_out[0, :] = y0
                continue

            retval = TOO_MUCH_WORK
            while retval == TOO_MUCH_WORK:
                retval = CVodeF(ode, t, state_c_ptr, time_p, lib.CV_NORMAL, n_check)
                if retval != TOO_MUCH_WORK and retval != 0:
                    raise SolverError("Bad sundials return code while solving ode: %s (%s)"
                                      % (ERRORS[retval], retval))
            y_out[i, :] = state_data

    @property
    def state(self) -> np.ndarray:
        return self._state_buffer.data

    @property
    def n_states(self) -> int:
        return self.problem.n_states

    @property
    def n_params(self) -> int:
        return self.problem.n_params

    @property
    def current_order(self) -> int:
        return check(lib.CVodeGetCurrentOrder(self.c_ptr))


class Solver:
    def __init__(self, problem: Problem, *,
                 compute_sens: bool = False, abstol: float = 1e-10, reltol: float = 1e-10,
                 sens_mode: Optional[str] = None, scaling_factors: Optional[np.ndarray] = None,
                 constraints: Optional[np.ndarray] = None):
        self._problem = problem
        self._user_data = problem.make_user_data()

        n_states = self._problem.n_states
        n_params = self._problem.n_params

        self._state_buffer = sunode.empty_vector(n_states)
        self._state_buffer.data[:] = 0
        self._jac = check(lib.SUNDenseMatrix(n_states, n_states))
        self._constraints = constraints

        self._ode = check(lib.CVodeCreate(lib.CV_BDF))
        rhs = problem.make_sundials_rhs()
        check(lib.CVodeInit(self._ode, rhs.cffi, 0., self._state_buffer.c_ptr))

        self._set_tolerances(abstol, reltol)
        if self._constraints is not None:
            assert constraints.shape == (n_states,)
            self._constraints_vec = sunode.from_numpy(constraints)
            check(lib.CVodeSetConstraints(self._ode, self._constraints_vec.c_ptr))

        self._make_linsol()

        user_data_p = ffi.cast('void *', ffi.addressof(ffi.from_buffer(self._user_data.data)))
        check(lib.CVodeSetUserData(self._ode, user_data_p))

        self._compute_sens = compute_sens
        if compute_sens:
            sens_rhs = self._problem.make_sundials_sensitivity_rhs()
            self._init_sens(sens_rhs, sens_mode)

    def _make_linsol(self) -> None:
        linsolver = check(lib.SUNLinSol_Dense(self._state_buffer.c_ptr, self._jac))
        check(lib.CVodeSetLinearSolver(self._ode, linsolver, self._jac))

        self._jac_func = self._problem.make_sundials_jac_dense()
        check(lib.CVodeSetJacFn(self._ode, self._jac_func.cffi))

    def _init_sens(self, sens_rhs, sens_mode, scaling_factors=None) -> None:
        if sens_mode == 'simultaneous':
            sens_mode = lib.CV_SIMULTANEOUS
        elif sens_mode == 'staggered':
            sens_mode = lib.CV_STAGGERED
        elif sens_mode == 'staggered1':
            raise ValueError('staggered1 requires work.')
        else:
            raise ValueError('sens_mode must be one of "simultaneous" and "staggered".')

        self._sens_mode = sens_mode

        n_params = self._problem.n_params
        yS = check(lib.N_VCloneVectorArray(n_params, self._state_buffer.c_ptr))
        vecs = [sunode.basic.Vector(yS[i]) for i in range(n_params)]
        for vec in vecs:
            vec.data[:] = 0
        self._sens_buffer_array = yS
        self._sens_buffers = vecs

        check(lib.CVodeSensInit(self._ode, n_params, sens_mode, sens_rhs.cffi, yS))

        if scaling_factors is not None:
            if scaling_factors.shape != (n_params,):
                raise ValueError('Invalid shape of scaling_factors.')
            self._scaling_factors = scaling_factors
            NULL_D = ffi.cast('double *', 0)
            NULL_I = ffi.cast('int *', 0)
            pbar_p = ffi.cast('double *', ffi.addressof(ffi.from_buffer(scaling_factors.data)))
            check(lib.CVodeSetSensParams(ode, NULL_D, pbar_p, NULL_I))

        check(lib.CVodeSensEEtolerances(self._ode))  # TODO
        check(lib.CVodeSetSensErrCon(self._ode, 1))  # TODO

    def _set_tolerances(self, atol=None, rtol=None):
        atol = np.array(atol)
        rtol = np.array(rtol)
        if atol.ndim == 1 and rtol.ndim == 1:
            atol = sunode.from_numpy(atol)
            rtol = sunode.from_numpy(rtol)
            check(lib.CVodeVVtolerances(self._ode, rtol.c_ptr, atol.c_ptr))
        elif atol.ndim == 1 and rtol.ndim == 0:
            atol = sunode.from_numpy(atol)
            check(lib.CVodeSVtolerances(self._ode, rtol, atol.c_ptr))
        elif atol.ndim == 0 and rtol.ndim == 1:
            rtol = sunode.from_numpy(rtol)
            check(lib.CVodeVStolerances(self._ode, rtol.c_ptr, atol))
        elif atol.ndim == 0 and rtol.ndim == 0:
            check(lib.CVodeSStolerances(self._ode, rtol, atol))
        else:
            raise ValueError('Invalid tolerance.')
        self._atol = atol
        self._rtol = rtol

    def make_output_buffers(self, tvals):
        n_states = self._problem.n_states
        n_params = self._problem.n_params
        y_vals = np.zeros((len(tvals), n_states))
        if self._compute_sens:
            sens_vals = np.zeros((len(tvals), n_params, n_states))
            return y_vals, sens_vals
        return y_vals

    def as_xarray(self, tvals, out, sens_out=None, unstack_state=True, unstack_params=True):
        return self._problem.solution_to_xarray(
            tvals, out, self._user_data,
            sensitivity=sens_out,
            unstack_state=unstack_state, unstack_params=unstack_params
        )

    @property
    def params_dtype(self):
        return self.problem.params_dtype

    @property
    def derivative_params_dtype(self):
        return self.problem.params_subset.subset_dtype

    @property
    def remainder_params_dtype(self):
        return self._problem.params_subset.remainder.subset_dtype

    def set_params(self, params):
        self._problem.update_params(self._user_data, params)

    def get_params(self):
        return self._problem.extract_params(self._user_data)

    def set_derivative_params(self, params):
        self._problem.update_subset_params(self._user_data, params)

    def set_remaining_params(self, params):
        self._problem.update_remaining_params(self._user_data, params)

    def set_params_dict(self, params):
        data = self.get_params()
        _from_dict(data, params)
        self.set_params(data)

    def get_params_dict(self):
        return _as_dict(self.get_params())

    def set_params_array(self, params):
        self._problem.update_changeable(self._user_data, params)

    def get_params_array(self, out=None):
        return self._problem.extract_changeable(self._user_data, out=out)

    def solve(self, t0, tvals, y0, y_out, *, sens0=None, sens_out=None):
        if self._compute_sens and (sens0 is None or sens_out is None):
            raise ValueError('"sens_out" and "sens0" are required when computin sensitivities.')
        CVodeReInit = lib.CVodeReInit
        CVodeSensReInit = lib.CVodeSensReInit
        CVode = lib.CVode
        CVodeGetSens = lib.CVodeGetSens
        ode = self._ode
        TOO_MUCH_WORK = lib.CV_TOO_MUCH_WORK

        n_params = self._problem.n_params

        state_data = self._state_buffer.data
        state_c_ptr = self._state_buffer.c_ptr

        if self._compute_sens:
            sens_buffer_array = self._sens_buffer_array
            sens_data = tuple(buffer.data for buffer in self._sens_buffers)
            for i in range(n_params):
                sens_data[i][:] = sens0[i, :]

        state_data[:] = y0

        time_p = ffi.new('double*')
        time_p[0] = t0

        check(CVodeReInit(ode, t0, state_c_ptr))
        if self._compute_sens:
            check(CVodeSensReInit(ode, self._sens_mode, self._sens_buffer_array))

        for i, t in enumerate(tvals):
            if t == t0:
                y_out[0, :] = y0
                if self._compute_sens:
                    sens_out[0, :, :] = sens0
                continue

            retval = TOO_MUCH_WORK
            while retval == TOO_MUCH_WORK:
                retval = CVode(ode, t, state_c_ptr, time_p, lib.CV_NORMAL)
                if retval != TOO_MUCH_WORK and retval != 0:
                    raise SolverError("Bad sundials return code while solving ode: %s (%s)"
                                      % (ERRORS[retval], retval))
            y_out[i, :] = state_data

            if self._compute_sens:
                check(CVodeGetSens(ode, time_p, sens_buffer_array))
                for j in range(n_params):
                    sens_out[i, j, :] = sens_data[j]


class AdjointSolver:
    def __init__(self, problem, *,
                 abstol=1e-10, reltol=1e-10,
                 checkpoint_n=500, interpolation='polynomial', constraints=None):
        self._problem = problem

        n_states, n_params = problem.n_states, problem.n_params
        self._user_data = problem.make_user_data()

        self._state_buffer = sunode.empty_vector(n_states)
        self._state_buffer.data[:] = 0

        self._jac = check(lib.SUNDenseMatrix(n_states, n_states))
        self._jacB = check(lib.SUNDenseMatrix(n_states, n_states))

        rhs = problem.make_sundials_rhs()
        self._adj_rhs = problem.make_sundials_adjoint_rhs()
        self._quad_rhs = problem.make_sundials_adjoint_quad_rhs()
        self._rhs = problem.make_rhs()
        self._constraints = constraints

        self._ode = check(lib.CVodeCreate(lib.CV_BDF))
        check(lib.CVodeInit(self._ode, rhs.cffi, 0., self._state_buffer.c_ptr))

        self._set_tolerances(abstol, reltol)
        if self._constraints is not None:
            self._constraints = np.broadcast_to(constraints, (n_states,)).copy()
            self._constraints_vec = sunode.from_numpy(self._constraints)
            check(lib.CVodeSetConstraints(self._ode, self._constraints_vec.c_ptr))

        self._make_linsol()

        user_data_p = ffi.cast('void *', ffi.addressof(ffi.from_buffer(self._user_data.data)))
        check(lib.CVodeSetUserData(self._ode, user_data_p))

        if interpolation == 'polynomial':
            interpolation = lib.CV_POLYNOMIAL
        elif interpolation == 'hermite':
            interpolation = lib.CV_HERMITE
        else:
            assert False
        self._init_backward(checkpoint_n, interpolation)

    def _init_backward(self, checkpoint_n, interpolation):
        check(lib.CVodeAdjInit(self._ode, checkpoint_n, interpolation))

        # Initialized by CVodeCreateB
        backward_ode = ffi.new('int*')
        check(lib.CVodeCreateB(self._ode, lib.CV_BDF, backward_ode))
        self._odeB = backward_ode[0]

        self._state_bufferB = sunode.empty_vector(self._problem.n_states)
        check(lib.CVodeInitB(self._ode, self._odeB, self._adj_rhs.cffi, 0., self._state_bufferB.c_ptr))

        # TODO
        check(lib.CVodeSStolerancesB(self._ode, self._odeB, 1e-10, 1e-10))

        linsolver = check(lib.SUNLinSol_Dense(self._state_bufferB.c_ptr, self._jacB))
        check(lib.CVodeSetLinearSolverB(self._ode, self._odeB, linsolver, self._jacB))

        self._jac_funcB = self._problem.make_sundials_adjoint_jac_dense()
        check(lib.CVodeSetJacFnB(self._ode, self._odeB, self._jac_funcB.cffi))

        user_data_p = ffi.cast('void *', ffi.addressof(ffi.from_buffer(self._user_data.data)))
        check(lib.CVodeSetUserDataB(self._ode, self._odeB, user_data_p))

        self._quad_buffer = sunode.empty_vector(self._problem.n_params)
        self._quad_buffer_out = sunode.empty_vector(self._problem.n_params)
        check(lib.CVodeQuadInitB(self._ode, self._odeB, self._quad_rhs.cffi, self._quad_buffer.c_ptr))

        check(lib.CVodeQuadSStolerancesB(self._ode, self._odeB, 1e-10, 1e-10))
        check(lib.CVodeSetQuadErrConB(self._ode, self._odeB, 1))

    def _make_linsol(self):
        linsolver = check(lib.SUNLinSol_Dense(self._state_buffer.c_ptr, self._jac))
        check(lib.CVodeSetLinearSolver(self._ode, linsolver, self._jac))

        self._jac_func = self._problem.make_sundials_jac_dense()
        check(lib.CVodeSetJacFn(self._ode, self._jac_func.cffi))

    def _set_tolerances(self, atol=None, rtol=None):
        atol = np.array(atol)
        rtol = np.array(rtol)
        if atol.ndim == 1 and rtol.ndim == 0:
            atol = sunode.from_numpy(atol)
            check(lib.CVodeSVtolerances(self._ode, rtol, atol.c_ptr))
        elif atol.ndim == 0 and rtol.ndim == 0:
            check(lib.CVodeSStolerances(self._ode, rtol, atol))
        else:
            raise ValueError('Invalid tolerance.')
        self._atol = atol
        self._rtol = rtol

    def make_output_buffers(self, tvals):
        y_vals = np.zeros((len(tvals), self._problem.n_states))
        grad_out = np.zeros(self._problem.n_params)
        lamda_out = np.zeros(self._problem.n_states)
        return y_vals, grad_out, lamda_out

    def as_xarray(self, tvals, out, sens_out=None, unstack_state=True, unstack_params=True):
        return self._problem.solution_to_xarray(
            tvals, out, self._user_data,
            sensitivity=sens_out,
            unstack_state=unstack_state, unstack_params=unstack_params
        )

    @property
    def params_dtype(self):
        return self._problem.params_dtype

    @property
    def derivative_params_dtype(self):
        return self._problem.params_subset.subset_dtype

    @property
    def remainder_params_dtype(self):
        return self._problem.params_subset.remainder.subset_dtype

    def set_params(self, params):
        self._problem.update_params(self._user_data, params)

    def get_params(self):
        return self._problem.extract_params(self._user_data)

    def set_params_dict(self, params):
        data = self.get_params()
        _from_dict(data, params)
        self.set_params(data)

    def get_params_dict(self):
        return _as_dict(self.get_params())

    def set_derivative_params(self, params):
        self._problem.update_subset_params(self._user_data, params)

    def set_remaining_params(self, params):
        self._problem.update_remaining_params(self._user_data, params)

    def solve_forward(self, t0, tvals, y0, y_out):
        CVodeReInit = lib.CVodeReInit
        CVodeAdjReInit = lib.CVodeAdjReInit
        CVodeF = lib.CVodeF
        ode = self._ode
        TOO_MUCH_WORK = lib.CV_TOO_MUCH_WORK

        state_data = self._state_buffer.data
        state_c_ptr = self._state_buffer.c_ptr

        state_data[:] = y0

        time_p = ffi.new('double*')
        time_p[0] = t0

        n_check = ffi.new('int*')
        n_check[0] = 0

        check(CVodeReInit(ode, t0, state_c_ptr))
        check(CVodeAdjReInit(ode))

        for i, t in enumerate(tvals):
            if t == t0:
                y_out[0, :] = y0
                continue

            retval = TOO_MUCH_WORK
            while retval == TOO_MUCH_WORK:
                retval = CVodeF(ode, t, state_c_ptr, time_p, lib.CV_NORMAL, n_check)
                if retval != TOO_MUCH_WORK and retval != 0:
                    raise SolverError("Bad sundials return code while solving ode: %s (%s)"
                                      % (ERRORS[retval], retval))
            y_out[i, :] = state_data

    def solve_backward(self, t0, tend, tvals, grads, grad_out, lamda_out,
                       lamda_all_out=None, quad_all_out=None, max_retries=50):
        CVodeReInitB = lib.CVodeReInitB
        CVodeQuadReInitB = lib.CVodeQuadReInitB
        CVodeGetQuadB = lib.CVodeGetQuadB
        CVodeB = lib.CVodeB
        CVodeGetB = lib.CVodeGetB
        ode = self._ode
        odeB = self._odeB
        TOO_MUCH_WORK = lib.CV_TOO_MUCH_WORK

        state_data = self._state_bufferB.data
        state_c_ptr = self._state_bufferB.c_ptr

        quad_data = self._quad_buffer.data
        quad_c_ptr = self._quad_buffer.c_ptr

        quad_out_data = self._quad_buffer_out.data
        quad_out_c_ptr = self._quad_buffer_out.c_ptr

        state_data[:] = 0
        quad_data[:] = 0
        quad_out_data[:] = 0

        time_p = ffi.new('double*')
        time_p[0] = t0

        ts = [t0] + list(tvals[::-1]) + [tend]
        t_intervals = zip(ts[1:], ts[:-1])
        grads = [None] + list(grads)

        for i, ((t_lower, t_upper), grad) in enumerate(zip(t_intervals, reversed(grads))):
            if t_lower < t_upper:
                check(CVodeReInitB(ode, odeB, t_upper, state_c_ptr))
                check(CVodeQuadReInitB(ode, odeB, quad_c_ptr))

                for retry in range(max_retries):
                    retval = CVodeB(ode, t_lower, lib.CV_NORMAL)
                    if retval == 0:
                        break
                    if retval != TOO_MUCH_WORK:
                        error = ERRORS[retval]
                        raise SolverError(f"Solving ode failed between time {t_upper} and "
                                          f"{t_lower}: {error} ({retval})")
                else:
                    raise SolverError(f"Too many solver retries between time {t_upper} and {t_lower}.")

                check(CVodeGetB(ode, odeB, time_p, state_c_ptr))
                check(CVodeGetQuadB(ode, odeB, time_p, quad_out_c_ptr))
                quad_data[:] = quad_out_data[:]
                assert time_p[0] == t_lower, (time_p[0], t_lower)

            if grad is not None:
                state_data[:] -= grad

                if lamda_all_out is not None:
                    lamda_all_out[-i, :] = state_data
                if quad_all_out is not None:
                    quad_all_out[-i, :] = quad_data

        grad_out[:] = quad_out_data
        lamda_out[:] = state_data
