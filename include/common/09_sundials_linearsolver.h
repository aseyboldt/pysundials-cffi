/* -----------------------------------------------------------------
 * Programmer(s): Daniel Reynolds @ SMU
 *                David Gardner, Carol Woodward, Slaven Peles @ LLNL
 * -----------------------------------------------------------------
 * SUNDIALS Copyright Start
 * Copyright (c) 2002-2019, Lawrence Livermore National Security
 * and Southern Methodist University.
 * All rights reserved.
 *
 * See the top-level LICENSE and NOTICE files for details.
 *
 * SPDX-License-Identifier: BSD-3-Clause
 * SUNDIALS Copyright End
 * -----------------------------------------------------------------
 * This is the header file for a generic linear solver package.
 * It defines the SUNLinearSolver structure (_generic_SUNLinearSolver)
 * which contains the following fields:
 *   - an implementation-dependent 'content' field which contains
 *     any internal data required by the solver
 *   - an 'ops' filed which contains a structure listing operations
 *     acting on/by such solvers
 *
 * We consider both direct linear solvers and iterative linear solvers
 * as available implementations of this package.  Furthermore, iterative
 * linear solvers can either use a matrix or be matrix-free.  As a
 * result of these different solver characteristics, some of the
 * routines are applicable only to some types of linear solver.
 * -----------------------------------------------------------------
 * This header file contains:
 *   - enumeration constants for all SUNDIALS-defined linear solver
 *     types, as well as a generic type for user-supplied linear
 *     solver types,
 *   - type declarations for the _generic_SUNLinearSolver and
 *     _generic_SUNLinearSolver_Ops structures, as well as references
 *     to pointers to such structures (SUNLinearSolver),
 *   - prototypes for the linear solver functions which operate
 *     on/by SUNLinearSolver objects, and
 *   - return codes for SUNLinearSolver objects.
 * -----------------------------------------------------------------
 * At a minimum, a particular implementation of a SUNLinearSolver must
 * do the following:
 *   - specify the 'content' field of SUNLinearSolver,
 *   - implement the operations on/by those SUNLinearSolver objects,
 *   - provide a constructor routine for new SUNLinearSolver objects
 *
 * Additionally, a SUNLinearSolver implementation may provide the
 * following:
 *   - "Set" routines to control solver-specific parameters/options
 *   - "Get" routines to access solver-specific performance metrics
 * -----------------------------------------------------------------*/


/* -----------------------------------------------------------------
 * Implemented SUNLinearSolver types:
 * ----------------------------------------------------------------- */

typedef enum {
  SUNLINEARSOLVER_DIRECT,
  SUNLINEARSOLVER_ITERATIVE,
  SUNLINEARSOLVER_MATRIX_ITERATIVE
} SUNLinearSolver_Type;



/* Forward reference for pointer to SUNLinearSolver object */
typedef ... *SUNLinearSolver;


/* -----------------------------------------------------------------
 * Functions exported by SUNLinearSolver module
 * ----------------------------------------------------------------- */

SUNLinearSolver_Type SUNLinSolGetType(SUNLinearSolver S);
int SUNLinSolSetATimes(SUNLinearSolver S, void* A_data, ATimesFn ATimes);
int SUNLinSolSetPreconditioner(SUNLinearSolver S, void* P_data, PSetupFn Pset, PSolveFn Psol);
int SUNLinSolSetScalingVectors(SUNLinearSolver S, N_Vector s1, N_Vector s2);
int SUNLinSolInitialize(SUNLinearSolver S);
int SUNLinSolSetup(SUNLinearSolver S, SUNMatrix A);
int SUNLinSolSolve(SUNLinearSolver S, SUNMatrix A, N_Vector x, N_Vector b, realtype tol);
int SUNLinSolNumIters(SUNLinearSolver S);
realtype SUNLinSolResNorm(SUNLinearSolver S);
N_Vector SUNLinSolResid(SUNLinearSolver S);
long int SUNLinSolLastFlag(SUNLinearSolver S);
int SUNLinSolSpace(SUNLinearSolver S, long int *lenrwLS, long int *leniwLS);
int SUNLinSolFree(SUNLinearSolver S);


/* -----------------------------------------------------------------
 * SUNLinearSolver return values
 * ----------------------------------------------------------------- */

#define SUNLS_SUCCESS             0   /* successful/converged          */

#define SUNLS_MEM_NULL           -801   /* mem argument is NULL          */
#define SUNLS_ILL_INPUT          -802   /* illegal function input        */
#define SUNLS_MEM_FAIL           -803   /* failed memory access          */
#define SUNLS_ATIMES_FAIL_UNREC  -804   /* atimes unrecoverable failure  */
#define SUNLS_PSET_FAIL_UNREC    -805   /* pset unrecoverable failure    */
#define SUNLS_PSOLVE_FAIL_UNREC  -806   /* psolve unrecoverable failure  */
#define SUNLS_PACKAGE_FAIL_UNREC -807   /* external package unrec. fail  */
#define SUNLS_GS_FAIL            -808   /* Gram-Schmidt failure          */
#define SUNLS_QRSOL_FAIL         -809   /* QRsol found singular R        */
#define SUNLS_VECTOROP_ERR       -810  /* vector operation error        */

#define SUNLS_RES_REDUCED         801   /* nonconv. solve, resid reduced */
#define SUNLS_CONV_FAIL           802   /* nonconvergent solve           */
#define SUNLS_ATIMES_FAIL_REC     803   /* atimes failed recoverably     */
#define SUNLS_PSET_FAIL_REC       804   /* pset failed recoverably       */
#define SUNLS_PSOLVE_FAIL_REC     805   /* psolve failed recoverably     */
#define SUNLS_PACKAGE_FAIL_REC    806   /* external package recov. fail  */
#define SUNLS_QRFACT_FAIL         807   /* QRfact found singular matrix  */
#define SUNLS_LUFACT_FAIL         808   /* LUfact found singular matrix  */
