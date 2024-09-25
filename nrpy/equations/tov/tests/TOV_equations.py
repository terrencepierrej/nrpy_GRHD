from mpmath import mpc, mpf  # type: ignore

trusted_dict = {
    "M": mpf("0.818896795274906330597275427862769"),
    "M_PI": mpf("3.14159265358979323846264338327933"),
    "P": mpf("0.669136818691102752687527299713111"),
    "dM_dr": mpf("0.0181392176313686643390698883937952"),
    "dP_dr": mpf("4.66855058947693486519180142696659"),
    "dnu_dr": mpf("-13.9032468056448406461804958524378"),
    "dr_iso_dr": mpc(real="0.0", imag="-0.765489710462761767259500476300972"),
    "r_Schw": mpf("0.769114044745645819567414491757518"),
    "r_iso": mpf("0.625697653400116182709211898327339"),
    "rho_energy": mpf("0.00244021000430416634685570898000151"),
}