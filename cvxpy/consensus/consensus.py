"""
Copyright 2018 Anqi Fu

This file is part of CVXPY.

CVXPY is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXPY is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXPY.  If not, see <http://www.gnu.org/licenses/>.
"""

import cvxpy.settings as s
from cvxpy.problems.problem import Problem, Minimize
from cvxpy.expressions.constants import Parameter
from cvxpy.atoms import sum_squares

import numpy as np
from time import time
from collections import defaultdict
from multiprocessing import Process, Pipe

def flip_obj(prob):
	"""Helper function to flip sign of objective function.
	"""
	if isinstance(prob.objective, Minimize):
		return prob.objective
	else:
		return -prob.objective

# Spectral step size.
def step_ls(p, d):
	"""Least squares estimator for spectral step size.
	
	Parameters
	----------
	p : array
	     Change in primal variable.
	d : array
	     Change in dual variable.
	
	Returns
	----------
	float
	     The least squares estimate.
	"""
	sd = np.sum(d**2)/np.sum(p*d)   # Steepest descent
	mg = np.sum(p*d)/np.sum(p**2)   # Minimum gradient
	
	if 2*mg > sd:
		return mg
	else:
		return (sd - mg)

def step_cor(p, d):
	"""Correlation coefficient.
	
	Parameters
	----------
	p : array
	     First vector.
	d : array
	     Second vector.
	
	Returns
	----------
	float
	     The correlation between two vectors.
	"""
	return np.sum(p*d)/np.sqrt(np.sum(p**2)*np.sum(d**2))

def step_safe(rho, a, b, a_cor, b_cor, eps = 0.2):
	"""Safeguarding rule for spectral step size update.
	
	Parameters
	----------
    rho : float
        The current step size.
    a : float
        Reciprocal of the curvature parameter alpha.
    b : float
        Reciprocal of the curvature parameter beta.
    a_cor : float
        Correlation of the curvature parameter alpha.
    b_cor : float
        Correlation of the curvature parameter beta.
    eps : float, optional
        The safeguarding threshold.
	"""
	if a_cor > eps and b_cor > eps:
		return np.sqrt(a*b)
	elif a_cor > eps and b_cor <= eps:
		return a
	elif a_cor <= eps and b_cor > eps:
		return b
	else:
		return rho

def step_spec(rho, k, dx, dxbar, du, duhat, eps = 0.2, C = 1e10):
	"""Calculates the generalized spectral step size with safeguarding.
	Xu, Taylor, et al. "Adaptive Consensus ADMM for Distributed Optimization."
	
	Parameters
    ----------
    rho : float
        The current step size.
    k : int
        The current iteration.
    dx : array
        Change in primal value from the last step size update.
    dxbar : array
        Change in average primal value from the last step size update.
    du : array
        Change in dual value from the last step size update.
    duhat : array
        Change in intermediate dual value from the last step size update.
    eps : float, optional
        The safeguarding threshold.
    C : float, optional
        The convergence constant.
    
    Returns
    ----------
    float
        The spectral step size for the next iteration.
	"""
	# Use old step size if unable to solve LS problem/correlations.
	if sum(dx**2) == 0 or sum(dxbar**2) == 0 or \
	   sum(du**2) == 0 or sum(duhat**2) == 0:
		   return rho

	# Compute spectral step size.
	a_hat = step_ls(dx, duhat)
	b_hat = step_ls(dxbar, du)
	
	# Estimate correlations.
	a_cor = step_cor(dx, duhat)
	b_cor = step_cor(dxbar, du)
	
	# Apply safeguarding rule.
	scale = 1 + C/(1.0*k**2)
	rho_hat = step_safe(rho, a_hat, b_hat, a_cor, b_cor, eps)
	return max(min(rho_hat, scale*rho), rho/scale)

def prox_step(prob, rho_init):
	vmap = {}   # Store consensus variables
	f = flip_obj(prob).args[0]
	rho = Parameter(1, 1, value = rho_init, sign = "positive")   # Step size
	
	# Add penalty for each variable.
	for xvar in prob.variables():
		xid = xvar.id
		size = xvar.size
		vmap[xid] = {"x": xvar, "xbar": Parameter(size[0], size[1], value = np.zeros(size)),
				     "u": Parameter(size[0], size[1], value = np.zeros(size))}
		f += (rho/2.0)*sum_squares(xvar - vmap[xid]["xbar"] - vmap[xid]["u"]/rho)
	
	prox = Problem(Minimize(f), prob.constraints)
	return prox, vmap, rho

def x_average(prox_res):
	xbars = defaultdict(float)
	xcnts = defaultdict(int)
	
	for status, xvals in prox_res:
		# Check if proximal step converged.
		if status in s.INF_OR_UNB:
			raise RuntimeError("Proximal problem is infeasible or unbounded")
		
		# Sum up x values.
		for key, value in xvals.items():
			xbars[key] += value
			++xcnts[key]
	
	# Divide by total count.
	for key in xbars.keys():
		if xcnts[key] != 0:
			xbars[key] /= xcnts[key]
	return xbars

def run_worker(pipe, p, rho_init, *args, **kwargs):
	# Spectral step size parameters.
	spectral = kwargs.pop("spectral", False)
	Tf = kwargs.pop("Tf", 2)
	eps = kwargs.pop("eps", 0.2)
	C = kwargs.pop("C", 1e10)
	
	# Initiate proximal problem.
	prox, v, rho = prox_step(p, rho_init)
	
	# Initiate step size variables.
	nelem = np.prod([np.prod(xvar.size) for xvar in p.variables()])
	v_old = {"x": np.zeros(nelem), "xbar": np.zeros(nelem),
			 "u": np.zeros(nelem), "uhat": np.zeros(nelem)}
	
	# ADMM loop.
	while True:
		prox.solve(*args, **kwargs)
		
		# Calculate x_bar.
		xvals = {}
		for xvar in prox.variables():
			xvals[xvar.id] = xvar.value
		pipe.send((prox.status, xvals))
		xbars, i = pipe.recv()
		
		# Update u += rho*(x - x_bar).
		v_flat = {"x": [], "xbar": [], "u": [], "uhat": []}
		for key in v.keys():
			xbar_old = v[key]["xbar"].value
			u_old = v[key]["u"].value
			
			v[key]["xbar"].value = xbars[key]
			v[key]["u"].value += (rho*(v[key]["x"] - v[key]["xbar"])).value
			
			# Intermediate variable for step size update.
			u_hat = u_old + rho*(xbar_old - v[key]["u"])
			v_flat["uhat"] += [np.asarray(u_hat.value).reshape(-1)]
		
		if spectral and i % Tf == 1:
			# Collect and flatten variables.
			for key in v.keys():
				v_flat["x"] += [np.asarray(v[key]["x"].value).reshape(-1)]
				v_flat["xbar"] += [np.asarray(v[key]["xbar"].value).reshape(-1)]
				v_flat["u"] += [np.asarray(v[key]["u"].value).reshape(-1)]
			
			for key in v_flat.keys():
				v_flat[key] = np.concatenate(v_flat[key])

			# Calculate change from old iterate.
			dx = v_flat["x"] - v_old["x"]
			dxbar = -v_flat["xbar"] + v_old["xbar"]
			du = v_flat["u"] - v_old["u"]
			duhat = v_flat["uhat"] - v_old["uhat"]
			
			# Update step size.
			rho.value = step_spec(rho.value, i, dx, dxbar, du, duhat, eps, C)
			
			# Update step size variables.
			for key in v_flat.keys():
				v_old[key] = v_flat[key]

def consensus(p_list, *args, **kwargs):
	N = len(p_list)   # Number of problems.
	max_iter = kwargs.pop("max_iter", 100)
	rho_init = kwargs.pop("rho_init", N*[1.0])
	
	# Set up the workers.
	pipes = []
	procs = []
	for i in range(N):
		local, remote = Pipe()
		pipes += [local]
		procs += [Process(target = run_worker, args = (remote, p_list[i], rho_init[i]) + args, kwargs = kwargs)]
		procs[-1].start()

	# ADMM loop.
	start = time()
	for i in range(max_iter):
		# Gather and average x_i.
		prox_res = [pipe.recv() for pipe in pipes]
		xbars = x_average(prox_res)
	
		# Scatter x_bar.
		for pipe in pipes:
			pipe.send((xbars, i))
	end = time()

	[p.terminate() for p in procs]
	return {"xbars": xbars, "solve_time": (end - start)}

def dicts_to_arr(xbars, udicts):
	# TODO: Flatten x_bar and u into vectors. (Keep original shape information).
	xstack = np.fromiter(xbars.values(), dtype=float, count=len(xbars))
	ustack = (np.fromiter(udict.values(), dtype=float, count=len(udict)) for udict in udicts)
	xuarr = np.concatenate(ustack + (xstack,))
	return np.array([xuarr]).T

def arr_to_dicts(arr, xids, xshapes):
	# Split array into x_bar and u vectors.
	xnum = len(xshapes)
	N = len(arr)/xnum - 1
	xelems = [np.prod(shape) for shape in xshapes]
	asubs = np.split(arr, N+1)
	
	# Reshape vectors into proper shape.
	sidx = 0
	xbars = []
	udicts = []
	for i in range(xnum):
		# Reshape x_bar.
		eidx = sidx + xelems[i]
		xvec = asubs[0][sidx:eidx]
		xbars += [np.reshape(xvec, xshapes[i])]
		
		# Reshape u_i for each pipe.
		uvals = []
		for j in range(1,N):
			uvec = asubs[j][sidx:eidx]
			uvals += [np.reshape(uvec, xshapes[i])]
		udicts += [uvals]
		sidx += xelems[i]
		
	xbars = dict(zip(xids, xbars))
	udicts = [dict(zip(xids, u)) for u in udicts]
	return xbars, udicts
	
def worker_map(pipe, p, rho_init, *args, **kwargs):
	# Initiate proximal problem.
	prox, v, rho = prox_step(p, rho_init)
	
	# ADMM loop.
	while True:
		# Set parameter values.
		xbars, uvals = pipe.recv()
		for key in v.keys():
			v[key]["xbar"].value = xbars[key]
			v[key]["u"].value = uvals[key]
		
		# Proximal step with given x_bar and u.
		prox.solve(*args, **kwargs)
		
		# Update u += rho*(x - x_bar).
		for key in v.keys():
			uvals[key] += rho.value*(v[key]["x"].value - xbars[key])
			
		# Scatter x and updated u.
		xvals = {k: d["x"].value for k,d in v.items()}
		pipe.send((prox.status, xvals, uvals))

def consensus_map(pipes, xbars, udicts):
	# Scatter x_bar and u.
	N = len(pipes)
	for i in range(N):
		pipes[i].send((xbars, udicts[i]))
	
	# Gather updated x and u.
	xbars_n = defaultdict(float)
	xcnts = defaultdict(int)
	udicts_n = []
	
	for i in range(N):
		status, xvals, uvals = pipes[i].recv()
		
		# Check if proximal step converged.
		if status in s.INF_OR_UNB:
			raise RuntimeError("Proximal problem is infeasible or unbounded")
		
		# Sum up x_i values.
		for key, value in xvals.items():
			xbars_n[key] += value
			++xcnts[key]
		udicts_n += [uvals]
	
	# Average x_i across pipes.
	for key in xbars.keys():
		if xcnts[key] != 0:
			xbars_n[key] /= xcnts[key]
	
	return xbars_n, udicts_n

def basic_test():
	from cvxpy import *
	np.random.seed(1)
	m = 100
	n = 10
	MAX_ITER = 10
	x = Variable(n)
	y = Variable(n/2)

	# Problem data.
	alpha = 0.5
	A = np.random.randn(m*n).reshape(m,n)
	xtrue = np.random.randn(n)
	b = A.dot(xtrue) + np.random.randn(m)

	# List of all the problems with objective f_i.
	p_list = [Problem(Minimize(sum_squares(A*x-b)), [norm(x,2) <= 1]),
			  Problem(Minimize((1-alpha)*sum_squares(y)/2))]
	N = len(p_list)
	rho_init = N*[1.0]
	
	# Set up the workers.
	pipes = []
	procs = []
	for i in range(N):
		local, remote = Pipe()
		pipes += [local]
		procs += [Process(target = worker_map, args = (remote, p_list[i], rho_init[i]))]
		procs[-1].start()
	
	# ADMM loop.
	xbars = {x.id: np.zeros(x.size), y.id: np.zeros(y.size)}
	udicts = N*[{x.id: np.zeros(x.size), y.id: np.zeros(y.size)}]
	for i in range(MAX_ITER):
		xbars, udicts = consensus_map(pipes, xbars, udicts)
	
	[p.terminate() for p in procs]
	for xid, xbar in xbars.items():
		print "Variable %d:\n" % xid, xbar

basic_test()
