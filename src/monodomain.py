from dolfinx import fem, mesh, io, geometry
import dolfinx.fem.petsc as petsc
from petsc4py import PETSc
import numpy as np
from mpi4py import MPI
import ufl
import gotranx
from pathlib import Path
from dataclasses import dataclass
import importlib
import sys
from scifem import evaluate_function

sys.path.append(str(Path(__file__).resolve().parents[1]))

def translateODE(odeFileName, schemes):
    odeFolder = str(Path.cwd().parent) + "/odes/"
    model_path = Path(odeFolder + odeFileName + ".py")
    if not model_path.is_file():
        ode = gotranx.load_ode(odeFolder + odeFileName + ".ode")
        code = gotranx.cli.gotran2py.get_code(ode, schemes)
        model_path.write_text(code)
    else:
        print("ODE already translated")

@dataclass
class PDESolver:
    h: float
    dt: float
    theta: float
    M: ufl.Constant    # M=1/(chi*C_m) * lambda/(1+lambda) * M_i

    def __post_init__(self)->None:
        self.N = int(np.ceil(1/self.h))

    def set_mesh(self, domain, lagrange_order) -> None:
        self.domain = domain

        self.V = fem.functionspace(domain, ("Lagrange", lagrange_order))
        self.t = fem.Constant(domain, 0.0)
        self.x = ufl.SpatialCoordinate(domain)

        self.vn = fem.Function(self.V)
        self.vn.name = "vn"
    
    def initialize_vn(self, initial_v):
        self.vn.interpolate(initial_v)
    
    def interpolate_func(self, func):
        fem_func = fem.Function(self.V)
        fem_func.interpolate(func)
        return fem_func

    def set_stimulus(self, I_stim):
        self.I_stim = I_stim(self.x, self.t)    # = 1/(chi*C_m) * I_stim
    
    def setup_solver(self):
        v = ufl.TrialFunction(self.V)
        phi = ufl.TestFunction(self.V)
        dx = ufl.dx(domain=self.domain)
        a = phi * v * dx + self.dt * self.theta * ufl.dot(ufl.grad(phi), self.M * ufl.grad(v)) * dx
        L = phi * (self.vn + self.dt * self.I_stim) * dx - self.dt * (1-self.theta) * ufl.dot(ufl.grad(phi), self.M * ufl.grad(self.vn)) * dx
        compiled_a = fem.form(a)
        A = petsc.assemble_matrix(compiled_a)
        A.assemble()

        self.compiled_L = fem.form(L)
        self.b = fem.Function(self.V)
        
        self.solver = PETSc.KSP().create(self.domain.comm)
        self.solver.setOperators(A)
        self.solver.setType(PETSc.KSP.Type.PREONLY)
        self.solver.getPC().setType(PETSc.PC.Type.LU)
    
    def solve_pde_step(self):
        self.b.x.array[:] = 0
        petsc.assemble_vector(self.b.vector, self.compiled_L)
        
        self.solver.solve(self.b.vector, self.vn.vector)
        self.vn.x.scatter_forward()


class ODESolver:
    def __init__(self, odefile, scheme, num_nodes, v_name = "v", initial_states = None):
        try:
            self.model = importlib.import_module(f"odes.{odefile}")
        except ImportError as e:
            raise ImportError(f"Failed to import {odefile}: {e}")

        if initial_states:
            init = self.model.init_state_values(**initial_states)
        else:
            init = self.model.init_state_values()
        self.states = np.tile(init, (num_nodes, 1)).T
        
        self.v_index = self.model.state_index(v_name)

        self.params = self.model.init_parameter_values()
        self.odesolver = getattr(self.model, scheme)

    def set_param(self, name, value):
        param_index = self.model.parameter_index(name)
        self.params[param_index] = value

    def set_state(self, state_name, state):
        state_index = self.model.state_index(state_name)
        self.states[state_index, :] = state[:]

    def solve_ode_step(self, t, dt):
        self.states[:] = self.odesolver(self.states, t, dt, self.params)

    def update_vn_array(self, vn):
        self.states[self.v_index, :] = vn.x.array[:]

    def get_vn(self):
        return self.states[self.v_index, :]

@dataclass
class MonodomainSolver:
    pde: PDESolver
    ode: ODESolver
    def __post_init__(self):
        self.t = self.pde.t
        self.dt = self.pde.dt
        self.theta = self.pde.theta
        self.domain = self.pde.domain

    def step(self):
        # Step 1
        self.ode.solve_ode_step(self.t.value, self.theta*self.dt)
        self.t.value += self.theta * self.dt

        # Step 2
        self.pde.vn.x.array[:] = self.ode.get_vn()
        self.pde.solve_pde_step()
        self.ode.update_vn_array(self.pde.vn)

        # Step 3
        if self.theta < 1.0:
            self.ode.solve_ode_step(self.t.value, (1 - self.theta)*self.dt)
            self.t.value += (1 - self.theta) * self.dt

        self.pde.vn.x.array[:] = self.ode.get_vn()

    def solve(self, T, vtx_title=None):
        if vtx_title:
            vtx = io.VTXWriter(MPI.COMM_WORLD, vtx_title + ".bp", [self.pde.vn], engine="BP4")
        while self.t.value < T + self.dt:
            self.step()
            if vtx_title:
                vtx.write(self.t.value)

        return self.pde.vn, self.pde.x, self.t
    
    def solve_num_steps(self, num_steps):
        for _ in range(num_steps):
            self.step()
        return self.pde.vn, self.pde.x, self.t
    
    def solve_activation_times(self, points, T):
        times = -np.ones(len(points))
        while self.t.value <= T and np.min(times) < 0:
            self.step()
            for i in range(len(points)):
                if times[i] < 0 and evaluate_function(self.pde.vn, [points[i]]) > 0:
                    times[i] = self.t.value
        return times