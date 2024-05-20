"""
Register function to zero out _rhs grid functions.

Author: Zachariah B. Etienne
        zachetie **at** gmail **dot* com
"""

import nrpy.c_function as cfc
import nrpy.grid as gri
import nrpy.infrastructures.ETLegacy.simple_loop as lp


def register_CFunction_zero_rhss(thorn_name: str) -> None:
    """
    Initialize RHSs of gridfunctions to zero for a NRPy+ generated Cactus thorn.

    :param thorn_name: The name of the thorn generated by NRPy+.
    """
    includes = ["cctk.h", "cctk_Arguments.h", "cctk_Parameters.h"]
    desc = f"Zero RHSs for NRPy+-generated thorn {thorn_name}"
    cfunc_type = "void"
    name = f"{thorn_name}_zero_rhss"
    params = "CCTK_ARGUMENTS"
    body = f"""  DECLARE_CCTK_ARGUMENTS_{name};
  DECLARE_CCTK_PARAMETERS;
"""

    set_rhss_to_zero = ""
    for gfname, gf in gri.glb_gridfcs_dict.items():
        if gf.group == "EVOL":
            gf_access = gri.ETLegacyGridFunction.access_gf(f"{gfname}_rhs")
            set_rhss_to_zero += f"{gf_access} = 0.0;\n"
    set_rhss_to_zero = set_rhss_to_zero.rstrip()

    body += lp.simple_loop(
        loop_body=set_rhss_to_zero,
        enable_simd=False,
        loop_region="all points",
        enable_OpenMP=True,
    )

    ET_schedule_bin_entry = (
        "BASEGRID",
        """
schedule FUNC_NAME at BASEGRID after Symmetry_registration
{
  LANG: C
  WRITES: evol_variables_rhs(everywhere)
} "Idea from Lean: set all rhs functions to zero to prevent spurious nans"
""",
    )

    cfc.register_CFunction(
        subdirectory=thorn_name,
        includes=includes,
        desc=desc,
        cfunc_type=cfunc_type,
        name=name,
        params=params,
        body=body,
        ET_thorn_name=thorn_name,
        ET_schedule_bins_entries=[ET_schedule_bin_entry],
    )
