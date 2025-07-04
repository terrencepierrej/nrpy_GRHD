"""
Black hole spectroscopy example.

Specifically, evolve Brill-Lindquist initial data forward in time
  and monitors the ringing of the merged black hole over time via
  psi4.
This example sets up a complete C code for solving the GR field
  equations in curvilinear coordinates on a cell-centered grid,
  using a reference metric approach.

Author: Zachariah B. Etienne
        zachetie **at** gmail **dot* com
"""

import argparse
import os

#########################################################
# STEP 1: Import needed Python modules, then set codegen
#         and compile-time parameters.
import shutil
from pathlib import Path

import nrpy.helpers.parallel_codegen as pcg
import nrpy.infrastructures.BHaH.BHaHAHA.interpolation_2d_general__uniform_src_grid as interpolation2d
import nrpy.infrastructures.BHaH.diagnostics.progress_indicator as progress
import nrpy.infrastructures.BHaH.parallelization.cuda_utilities as cudautils
import nrpy.infrastructures.BHaH.special_functions.spin_weight_minus2_spherical_harmonics as swm2sh
import nrpy.params as par
from nrpy.helpers.generic import copy_files
from nrpy.infrastructures.BHaH import (
    BHaH_defines_h,
    BHaH_device_defines_h,
    CodeParameters,
    CurviBoundaryConditions,
    Makefile_helpers,
    MoLtimestepping,
    checkpointing,
    cmdline_input_and_parfiles,
    griddata_commondata,
    main_c,
    numerical_grids_and_timestep,
    rfm_precompute,
    rfm_wrapper_functions,
    xx_tofrom_Cart,
)
from nrpy.infrastructures.BHaH.general_relativity import (
    BSSN,
    PSI4,
    NRPyPN_quasicircular_momenta,
    TwoPunctures,
)

parser = argparse.ArgumentParser(
    description="NRPyElliptic Solver for Conformally Flat BBH initial data"
)
parser.add_argument(
    "--parallelization",
    type=str,
    help="Parallelization strategy to use (e.g. openmp, cuda).",
    default="openmp",
)
parser.add_argument(
    "--floating_point_precision",
    type=str,
    help="Floating point precision (e.g. float, double).",
    default="double",
)
parser.add_argument(
    "--disable_intrinsics",
    action="store_true",
    help="Flag to disable hardware intrinsics",
    default=False,
)
parser.add_argument(
    "--disable_rfm_precompute",
    action="store_true",
    help="Flag to disable RFM precomputation.",
    default=False,
)
args = parser.parse_args()

# Code-generation-time parameters:
fp_type = args.floating_point_precision.lower()
parallelization = args.parallelization.lower()
enable_intrinsics = not args.disable_intrinsics
enable_rfm_precompute = not args.disable_rfm_precompute

if parallelization not in ["openmp", "cuda"]:
    raise ValueError(
        f"Invalid parallelization strategy: {parallelization}. "
        "Choose 'openmp' or 'cuda'."
    )

par.set_parval_from_str("Infrastructure", "BHaH")
par.set_parval_from_str("parallelization", parallelization)
par.set_parval_from_str("fp_type", fp_type)

# Code-generation-time parameters:
project_name = "blackhole_spectroscopy"
CoordSystem = "SinhCylindrical"
IDtype = "TP_Interp"
IDCoordSystem = "Cartesian"

initial_sep = 0.5
mass_ratio = 1.0  # must be >= 1.0. Will need higher resolution for > 1.0.
BH_m_chix = 0.0  # dimensionless spin parameter for less-massive BH
BH_M_chix = 0.0  # dimensionless spin parameter for more-massive BH
initial_p_r = 0.0  # want this to be <= 0.0. 0.0 -> fall from rest, < 0.0 -> boosted toward each other.
TP_npoints_A = 48
TP_npoints_B = 48
TP_npoints_phi = 4

enable_KreissOliger_dissipation = True
enable_CAKO = True
enable_CAHD = False
enable_SSL = True
KreissOliger_strength_gauge = 0.99
KreissOliger_strength_nongauge = 0.3
LapseEvolutionOption = "OnePlusLog"
ShiftEvolutionOption = "GammaDriving2ndOrder_Covariant"
GammaDriving_eta = 2.0
grid_physical_size = 300.0
diagnostics_output_every = 0.5
default_checkpoint_every = 2.0
t_final = 1.5 * grid_physical_size
swm2sh_maximum_l_mode_generated = 8
swm2sh_maximum_l_mode_to_compute = 2  # for consistency with NRPy 1.0 version.
Nxx_dict = {
    "SinhSpherical": [800, 16, 2],
    "SinhCylindrical": [400, 2, 1200],
}
default_BH1_mass = default_BH2_mass = 0.5
default_BH1_z_posn = +0.25
default_BH2_z_posn = -0.25
enable_rfm_precompute = True
MoL_method = "RK4"
fd_order = 8
radiation_BC_fd_order = 4
enable_intrinsics = True
separate_Ricci_and_BSSN_RHS = True
parallel_codegen_enable = True
enable_fd_functions = True
boundary_conditions_desc = "outgoing radiation"

set_of_CoordSystems = {CoordSystem}
NUMGRIDS = len(set_of_CoordSystems)
num_cuda_streams = NUMGRIDS
par.adjust_CodeParam_default("NUMGRIDS", NUMGRIDS)

OMP_collapse = 1
if "Spherical" in CoordSystem:
    par.set_parval_from_str("symmetry_axes", "2")
    par.adjust_CodeParam_default("CFL_FACTOR", 1.0)
    OMP_collapse = 2  # about 2x faster
    if CoordSystem == "SinhSpherical":
        sinh_width = 0.2
if "Cylindrical" in CoordSystem:
    par.set_parval_from_str("symmetry_axes", "1")
    par.adjust_CodeParam_default("CFL_FACTOR", 0.5)
    OMP_collapse = 2  # might be slightly faster
    if CoordSystem == "SinhCylindrical":
        sinh_width = 0.2

project_dir = os.path.join("project", project_name)

# First clean the project directory, if it exists.
shutil.rmtree(project_dir, ignore_errors=True)

# Set NRPy parameters that steer the code generation
par.set_parval_from_str("parallel_codegen_enable", parallel_codegen_enable)
par.set_parval_from_str("fd_order", fd_order)
par.set_parval_from_str("CoordSystem_to_register_CodeParameters", CoordSystem)
par.set_parval_from_str(
    "swm2sh_maximum_l_mode_generated", swm2sh_maximum_l_mode_generated
)

#########################################################
# STEP 2: Declare core C functions & register each to
#         cfc.CFunction_dict["function_name"]

if parallelization == "cuda":
    cudautils.register_CFunctions_HostDevice__operations()
    cudautils.register_CFunction_find_global_minimum()
    cudautils.register_CFunction_find_global_sum()

NRPyPN_quasicircular_momenta.register_CFunction_NRPyPN_quasicircular_momenta()
TwoPunctures.TwoPunctures_lib.register_C_functions()
BSSN.initial_data.register_CFunction_initial_data(
    CoordSystem=CoordSystem,
    IDtype=IDtype,
    IDCoordSystem=IDCoordSystem,
    enable_checkpointing=True,
    ID_persist_struct_str=TwoPunctures.ID_persist_struct.ID_persist_str(),
    populate_ID_persist_struct_str=r"""
initialize_ID_persist_struct(commondata, &ID_persist);
TP_solve(&ID_persist);
""",
    free_ID_persist_struct_str=r"""
{
  extern void free_derivs (derivs * v, int n);  // <- Needed to free memory allocated by TwoPunctures.
  // <- Free memory allocated within ID_persist.
  // Now that we're finished with par.v and par.cf_v (needed in setting up ID, we can free up memory for TwoPunctures' grids...
  free_derivs (&ID_persist.v,    ID_persist.npoints_A * ID_persist.npoints_B * ID_persist.npoints_phi);
  free_derivs (&ID_persist.cf_v, ID_persist.npoints_A * ID_persist.npoints_B * ID_persist.npoints_phi);
}
""",
)
interpolation2d.register_CFunction_interpolation_2d_general__uniform_src_grid(
    enable_simd=enable_intrinsics, project_dir=project_dir
)
BSSN.diagnostics.register_CFunction_diagnostics(
    set_of_CoordSystems=set_of_CoordSystems,
    default_diagnostics_out_every=diagnostics_output_every,
    enable_psi4_diagnostics=True,
    grid_center_filename_tuple=("out0d-conv_factor%.2f.txt", "convergence_factor"),
    axis_filename_tuple=(
        "out1d-AXIS-conv_factor%.2f-t%08.2f.txt",
        "convergence_factor, time",
    ),
    plane_filename_tuple=(
        "out2d-PLANE-conv_factor%.2f-t%08.2f.txt",
        "convergence_factor, time",
    ),
    out_quantities_dict="default",
)
if enable_rfm_precompute:
    rfm_precompute.register_CFunctions_rfm_precompute(
        set_of_CoordSystems=set_of_CoordSystems,
    )
BSSN.rhs_eval.register_CFunction_rhs_eval(
    CoordSystem=CoordSystem,
    enable_rfm_precompute=enable_rfm_precompute,
    enable_RbarDD_gridfunctions=separate_Ricci_and_BSSN_RHS,
    enable_T4munu=False,
    enable_intrinsics=enable_intrinsics,
    enable_fd_functions=enable_fd_functions,
    LapseEvolutionOption=LapseEvolutionOption,
    ShiftEvolutionOption=ShiftEvolutionOption,
    enable_KreissOliger_dissipation=enable_KreissOliger_dissipation,
    KreissOliger_strength_gauge=KreissOliger_strength_gauge,
    KreissOliger_strength_nongauge=KreissOliger_strength_nongauge,
    enable_CAKO=enable_CAKO,
    enable_CAHD=enable_CAHD,
    enable_SSL=enable_SSL,
    OMP_collapse=OMP_collapse,
)
if enable_CAHD:
    BSSN.cahdprefactor_gf.register_CFunction_cahdprefactor_auxevol_gridfunction(
        {CoordSystem}
    )
if separate_Ricci_and_BSSN_RHS:
    BSSN.Ricci_eval.register_CFunction_Ricci_eval(
        CoordSystem=CoordSystem,
        enable_rfm_precompute=enable_rfm_precompute,
        enable_intrinsics=enable_intrinsics,
        enable_fd_functions=enable_fd_functions,
        OMP_collapse=OMP_collapse,
    )
BSSN.enforce_detgammabar_equals_detgammahat.register_CFunction_enforce_detgammabar_equals_detgammahat(
    CoordSystem=CoordSystem,
    enable_rfm_precompute=enable_rfm_precompute,
    enable_fd_functions=enable_fd_functions,
    OMP_collapse=OMP_collapse,
)
BSSN.constraints.register_CFunction_constraints(
    CoordSystem=CoordSystem,
    enable_rfm_precompute=enable_rfm_precompute,
    enable_RbarDD_gridfunctions=separate_Ricci_and_BSSN_RHS,
    enable_T4munu=False,
    enable_intrinsics=enable_intrinsics,
    enable_fd_functions=enable_fd_functions,
    OMP_collapse=OMP_collapse,
)

PSI4.compute_psi4.register_CFunction_psi4(
    CoordSystem=CoordSystem,
    OMP_collapse=OMP_collapse,
    enable_fd_functions=enable_fd_functions,
)
swm2sh.register_CFunction_spin_weight_minus2_sph_harmonics()

if __name__ == "__main__":
    pcg.do_parallel_codegen()
# Does not need to be parallelized.
BSSN.psi4_decomposition.register_CFunction_psi4_spinweightm2_decomposition(
    CoordSystem=CoordSystem
)

numerical_grids_and_timestep.register_CFunctions(
    set_of_CoordSystems=set_of_CoordSystems,
    list_of_grid_physical_sizes=[grid_physical_size],
    Nxx_dict=Nxx_dict,
    enable_rfm_precompute=enable_rfm_precompute,
    enable_CurviBCs=True,
)

CurviBoundaryConditions.register_all.register_C_functions(
    set_of_CoordSystems=set_of_CoordSystems,
    radiation_BC_fd_order=radiation_BC_fd_order,
    set_parity_on_aux=True,
)

rhs_string = ""
if enable_SSL:
    rhs_string += """
// Set SSL strength (SSL_Gaussian_prefactor):
commondata->SSL_Gaussian_prefactor = commondata->SSL_h * exp(-commondata->time * commondata->time / (2 * commondata->SSL_sigma * commondata->SSL_sigma));
"""
if separate_Ricci_and_BSSN_RHS:
    rhs_string += "Ricci_eval(params, rfmstruct, RK_INPUT_GFS, auxevol_gfs);"
rhs_string += """
rhs_eval(commondata, params, rfmstruct, auxevol_gfs, RK_INPUT_GFS, RK_OUTPUT_GFS);
if (strncmp(commondata->outer_bc_type, "radiation", 50) == 0)
  apply_bcs_outerradiation_and_inner(commondata, params, bcstruct, griddata[grid].xx,
                                     gridfunctions_wavespeed,gridfunctions_f_infinity,
                                     RK_INPUT_GFS, RK_OUTPUT_GFS);"""
if not enable_rfm_precompute:
    rhs_string = rhs_string.replace("rfmstruct", "xx")

MoLtimestepping.register_all.register_CFunctions(
    MoL_method=MoL_method,
    rhs_string=rhs_string,
    post_rhs_string="""if (strncmp(commondata->outer_bc_type, "extrapolation", 50) == 0)
  apply_bcs_outerextrap_and_inner(commondata, params, bcstruct, RK_OUTPUT_GFS);
  enforce_detgammabar_equals_detgammahat(params, rfmstruct, RK_OUTPUT_GFS);""",
    enable_rfm_precompute=enable_rfm_precompute,
    enable_curviBCs=True,
)
xx_tofrom_Cart.register_CFunction__Cart_to_xx_and_nearest_i0i1i2(CoordSystem)
xx_tofrom_Cart.register_CFunction_xx_to_Cart(CoordSystem)
checkpointing.register_CFunctions(default_checkpoint_every=default_checkpoint_every)
progress.register_CFunction_progress_indicator()
rfm_wrapper_functions.register_CFunctions_CoordSystem_wrapper_funcs()

# Reset CodeParameter defaults according to variables set above.
# Coord system parameters
if CoordSystem == "SinhSpherical":
    par.adjust_CodeParam_default("SINHW", sinh_width)
if CoordSystem == "SinhCylindrical":
    par.adjust_CodeParam_default("AMPLRHO", grid_physical_size)
    par.adjust_CodeParam_default("AMPLZ", grid_physical_size)
    par.adjust_CodeParam_default("SINHWRHO", sinh_width)
    par.adjust_CodeParam_default("SINHWZ", sinh_width)
par.adjust_CodeParam_default("t_final", t_final)
# Initial data parameters
par.adjust_CodeParam_default("initial_sep", initial_sep)
par.adjust_CodeParam_default("mass_ratio", mass_ratio)
par.adjust_CodeParam_default("bbhxy_BH_m_chix", BH_m_chix)
par.adjust_CodeParam_default("bbhxy_BH_M_chix", BH_M_chix)
par.adjust_CodeParam_default("initial_p_t", 0.0)
par.adjust_CodeParam_default("initial_p_r", initial_p_r)
par.adjust_CodeParam_default("TP_npoints_A", TP_npoints_A)
par.adjust_CodeParam_default("TP_npoints_B", TP_npoints_B)
par.adjust_CodeParam_default("TP_npoints_phi", TP_npoints_phi)
par.adjust_CodeParam_default("TP_bare_mass_m", 1.0 / (1.0 + mass_ratio))
par.adjust_CodeParam_default("TP_bare_mass_M", mass_ratio / (1.0 + mass_ratio))
# Evolution / diagnostics parameters
par.adjust_CodeParam_default("eta", GammaDriving_eta)
par.adjust_CodeParam_default(
    "swm2sh_maximum_l_mode_to_compute", swm2sh_maximum_l_mode_to_compute
)

#########################################################
# STEP 3: Generate header files, register C functions and
#         command line parameters, set up boundary conditions,
#         and create a Makefile for this project.
#         Project is output to project/[project_name]/
CodeParameters.write_CodeParameters_h_files(project_dir=project_dir)
CodeParameters.register_CFunctions_params_commondata_struct_set_to_default()
cmdline_input_and_parfiles.generate_default_parfile(
    project_dir=project_dir, project_name=project_name
)
cmdline_input_and_parfiles.register_CFunction_cmdline_input_and_parfile_parser(
    project_name=project_name, cmdline_inputs=["convergence_factor"]
)
copy_files(
    package="nrpy.infrastructures.BHaH.general_relativity.TwoPunctures",
    filenames_list=["TwoPunctures.h", "TP_utilities.h"],
    project_dir=project_dir,
    subdirectory="TwoPunctures",
)
gpu_defines_filename = BHaH_device_defines_h.output_device_headers(
    project_dir,
    num_streams=num_cuda_streams,
    set_parity_on_aux=True,
)
BHaH_defines_h.output_BHaH_defines_h(
    additional_includes=[str(Path("TwoPunctures") / Path("TwoPunctures.h"))],
    project_dir=project_dir,
    enable_intrinsics=enable_intrinsics,
    intrinsics_header_lst=(
        ["cuda_intrinsics.h"] if parallelization == "cuda" else ["simd_intrinsics.h"]
    ),
    enable_rfm_precompute=enable_rfm_precompute,
    fin_NGHOSTS_add_one_for_upwinding_or_KO=True,
    DOUBLE_means="double" if fp_type == "float" else "REAL",
    restrict_pointer_type="*" if parallelization == "cuda" else "*restrict",
    supplemental_defines_dict=(
        {
            "C++/CUDA safe restrict": "#define restrict __restrict__",
            "GPU Header": f'#include "{gpu_defines_filename}"',
        }
        if parallelization == "cuda"
        else {}
    ),
)
post_non_y_n_auxevol_mallocs = ""
if enable_CAHD:
    post_non_y_n_auxevol_mallocs = """for(int grid=0; grid<commondata.NUMGRIDS; grid++) {
    cahdprefactor_auxevol_gridfunction(&commondata, &griddata[grid].params, griddata[grid].xx,  griddata[grid].gridfuncs.auxevol_gfs);
}\n""".replace(
        "griddata", "griddata_device" if parallelization == "cuda" else "griddata"
    )

# Set griddata struct used for calculations to griddata_device for certain parallelizations
compute_griddata = "griddata_device" if parallelization in ["cuda"] else "griddata"

# Define post_MoL_step_forward_in_time string for main function
write_checkpoint_call = f"write_checkpoint(&commondata, {compute_griddata});\n".replace(
    compute_griddata,
    (
        f"griddata_host, {compute_griddata}"
        if parallelization in ["cuda"]
        else compute_griddata
    ),
)

main_c.register_CFunction_main_c(
    MoL_method=MoL_method,
    initial_data_desc=IDtype,
    boundary_conditions_desc=boundary_conditions_desc,
    post_non_y_n_auxevol_mallocs=post_non_y_n_auxevol_mallocs,
    pre_MoL_step_forward_in_time=write_checkpoint_call,
)
griddata_commondata.register_CFunction_griddata_free(
    enable_rfm_precompute=enable_rfm_precompute, enable_CurviBCs=True
)

if enable_intrinsics:
    copy_files(
        package="nrpy.helpers",
        filenames_list=(
            ["cuda_intrinsics.h", "simd_intrinsics.h"]
            if parallelization == "cuda"
            else ["simd_intrinsics.h"]
        ),
        project_dir=project_dir,
        subdirectory="intrinsics",
    )

cuda_makefiles_options = (
    {
        "CC": "nvcc",
        "src_code_file_ext": "cu",
        "compiler_opt_option": "nvcc",
    }
    if parallelization == "cuda"
    else {}
)

if parallelization == "cuda":
    Makefile_helpers.output_CFunctions_function_prototypes_and_construct_Makefile(
        project_dir=project_dir,
        project_name=project_name,
        exec_or_library_name=project_name,
        CC="nvcc",
        src_code_file_ext="cu",
        compiler_opt_option="nvcc",
        addl_CFLAGS=["$(shell gsl-config --cflags)"],
        addl_libraries=["$(shell gsl-config --libs)"],
    )
else:
    Makefile_helpers.output_CFunctions_function_prototypes_and_construct_Makefile(
        project_dir=project_dir,
        project_name=project_name,
        exec_or_library_name=project_name,
        compiler_opt_option="default",
        addl_CFLAGS=["$(shell gsl-config --cflags)"],
        addl_libraries=["$(shell gsl-config --libs)"],
    )
print(
    f"Finished! Now go into project/{project_name} and type `make` to build, then ./{project_name} to run."
)
print(f"    Parameter file can be found in {project_name}.par")
