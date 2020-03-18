import theano
import theano.tensor as tt
import copy
from typing import Dict, Optional, Any, Callable

import numpy as np
import pandas as pd
import sympy as sym

from sunode.solver import SolverError
from sunode.dtypesubset import as_flattened
from sunode import basic, symode, solver
import sunode.symode.problem
from sunode.symode.problem import SympyProblem


def solve_ivp(
    t0: float,
    y0: np.ndarray,
    params: Dict[str, Any],
    tvals: np.ndarray,
    rhs: Callable[[sym.Symbol, np.ndarray, np.ndarray], Dict[str, Any]],
    derivatives: str = 'adjoint',
    coords: Optional[Dict[str, pd.Index]] = None,
    make_solver=None,
    derivative_subset=None,
    solver_kwargs=None,
    simplify=None,
) -> Any:
    dtype = basic.data_dtype
    if solver_kwargs is None:
        solver_kwargs={}

    def read_dict(vals):
        if isinstance(vals, dict):
            return {name: read_dict(item) for name, item in vals.items()}
        else:
            if isinstance(vals, tuple):
                tensor, dim_names = vals
            else:
                try:
                    tensor, dim_names = vals, tt.as_tensor_variable(vals).shape.eval()
                except theano.gof.MissingInputError as e:
                    raise ValueError(
                        'Shapes of tensors need to be statically '
                        'known or given explicitly.') from e
            if isinstance(dim_names, (str, int)):
                dim_names = (dim_names,)
            tensor = tt.as_tensor_variable(tensor)
            assert tensor.ndim == len(dim_names)
            assert np.dtype(tensor.dtype) == dtype, tensor
            return dim_names

    y0_dims = read_dict(y0)
    params_dims = read_dict(params)

    if derivative_subset is None:
        derivative_subset = []
        for path, val in as_flattened(params).items():
            if isinstance(val, tuple):
                tensor, _ = val
            else:
                tensor = val
            if isinstance(tensor, tt.Variable):
                if not isinstance(tensor, tt.Constant):
                    derivative_subset.append(path)

    problem = symode.problem.SympyProblem(
        params_dims, y0_dims, rhs, derivative_subset, coords=coords, simplify=simplify)

    flat_tensors = as_flattened(params)
    vars = []
    for path in problem.params_subset.subset_paths:
        tensor = flat_tensors[path]
        if isinstance(tensor, tuple):
            tensor, _ = tensor
        vars.append(tt.as_tensor_variable(tensor).reshape((-1,)))
    if vars:
        params_subs_flat = tt.concatenate(vars)
    else:
        params_subs_flat = tt.as_tensor_variable(np.zeros(0))

    vars = []
    for path in problem.params_subset.remainder.subset_paths:
        tensor = flat_tensors[path]
        if isinstance(tensor, tuple):
            tensor, _ = tensor
        vars.append(tt.as_tensor_variable(tensor).reshape((-1,)))
    if vars:
        params_remaining_flat = tt.concatenate(vars)
    else:
        params_remaining_flat = tt.as_tensor_variable(np.zeros(0))

    flat_tensors = as_flattened(y0)
    vars = []
    for path in problem.state_subset.paths:
        tensor = flat_tensors[path]
        if isinstance(tensor, tuple):
            tensor, _ = tensor
        vars.append(tt.as_tensor_variable(tensor).reshape((-1,)))
    y0_flat = tt.concatenate(vars)

    if derivatives == 'adjoint':
        sol = solver.AdjointSolver(problem, **solver_kwargs)
        wrapper = SolveODEAdjoint(sol, t0, tvals)
        flat_solution = wrapper(y0_flat, params_subs_flat, params_remaining_flat)
        solution = problem.flat_solution_as_dict(flat_solution)
        return solution, flat_solution, problem, sol, y0_flat, params_subs_flat
    elif derivatives == 'forward':
        sol = solver.Solver(problem)
        wrapper = sol.SolveODE(sol, t0, tvals)
        return wrapper(y0_flat, params_subs_flat, params_remaining_flat)[0], problem, sol
    elif derivatives in [None, False]:
        sol = solver.Solver(problem, sens_mode=False)
        assert False


class SolveODE(tt.Op):
    itypes = [tt.dvector, tt.dvector, tt.dvector]
    otypes = [tt.dmatrix, tt.dtensor3]
    
    __props__ = ('_solver_id', '_t0', '_tvals_id')
    
    def __init__(self, solver, t0, tvals):
        self._solver = solver
        self._t0 = t0
        self._y_out, self._sens_out = solver.make_output_buffers(tvals)
        self._tvals = tvals
        self._solver_id = id(solver)
        self._tvals_id = id(self._tvals)
        self._deriv_dtype = self._solver.derivative_params_dtype
        self._fixed_dtype = self._solver.remainder_params_dtype

    def perform(self, node, inputs, outputs):
        y0, params, params_fixed = inputs

        self._solver.set_derivative_params(params.view(self._deriv_dtype)[0])
        self._solver.set_remaining_params(params_fixed.view(self._fixed_dtype)[0])
        self._solver.solve(self._t0, self._tvals, y0, self._y_out,
                           sens0=sens0, sens_out=self._sens_out)
        outputs[0][0] = self._y_out
        outputs[1][0] = self._sens_out

    def grad(self, inputs, g):
        g, g_grad = g
        
        assert str(g_grad) == '<DisconnectedType>'
        params, = inputs
        solution, sens = self(params)
        return [tt.sum(g[:, None, :] * sens, (0, -1))]


class SolveODEAdjoint(tt.Op):
    itypes = [tt.dvector, tt.dvector, tt.dvector]
    otypes = [tt.dmatrix]

    __props__ = ('_solver_id', '_t0', '_tvals_id')

    def __init__(self, solver, t0, tvals):
        self._solver = solver
        self._t0 = t0
        self._y_out, self._grad_out, self._lamda_out = solver.make_output_buffers(tvals)
        self._tvals = tvals
        self._solver_id = id(solver)
        self._tvals_id = id(self._tvals)
        self._deriv_dtype = self._solver.derivative_params_dtype
        self._fixed_dtype = self._solver.remainder_params_dtype

    def perform(self, node, inputs, outputs):
        y0, params, params_fixed = inputs

        self._solver.set_derivative_params(params.view(self._deriv_dtype)[0])
        self._solver.set_remaining_params(params_fixed.view(self._fixed_dtype)[0])

        try:
            self._solver.solve_forward(self._t0, self._tvals, y0, self._y_out)
        except SolverError as e:
            self._y_out[:] = np.nan

        outputs[0][0] = self._y_out.copy()

    def grad(self, inputs, g):
        g, = g

        y0, params, params_fixed = inputs
        backward = SolveODEAdjointBackward(self._solver, self._t0, self._tvals)
        lamda, gradient = backward(y0, params, params_fixed, g)
        return [-lamda, gradient, tt.grad_not_implemented(self, 2, params_fixed)]


class SolveODEAdjointBackward(tt.Op):
    itypes = [tt.dvector, tt.dvector, tt.dvector, tt.dmatrix]
    otypes = [tt.dvector, tt.dvector]

    __props__ = ('_solver_id', '_t0', '_tvals_id')

    def __init__(self, solver, t0, tvals):
        self._solver = solver
        self._t0 = t0
        self._y_out, self._grad_out, self._lamda_out = solver.make_output_buffers(tvals)
        self._tvals = tvals
        self._solver_id = id(solver)
        self._tvals_id = id(self._tvals)
        self._deriv_dtype = self._solver.derivative_params_dtype
        self._fixed_dtype = self._solver.remainder_params_dtype

    def perform(self, node, inputs, outputs):
        y0, params, params_fixed, grads = inputs

        self._solver.set_derivative_params(params.view(self._deriv_dtype)[0])
        self._solver.set_remaining_params(params_fixed.view(self._fixed_dtype)[0])

        # TODO We don't really need to do the forward solve if we make sure
        # that it was executed previously, but it isn't very expensive
        # compared with the backward pass anyway.
        try:
            self._solver.solve_forward(self._t0, self._tvals, y0, self._y_out)
            self._solver.solve_backward(self._tvals[-1], self._t0, self._tvals,
                                        grads, self._grad_out, self._lamda_out)
        except SolverError as e:
            self._lamda_out[:] = np.nan
            self._grad_out[:] = np.nan

        outputs[0][0] = self._lamda_out.copy()
        outputs[1][0] = self._grad_out.copy()
