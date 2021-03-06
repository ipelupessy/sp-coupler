from __future__ import print_function

import time
import numpy
import logging
from amuse.community import units
import sputils
import spio
import scipy.optimize

# Logger
log = logging.getLogger(__name__)


# Superparametrization coupling methods

# Retrieves the les model cloud fraction
def get_cloud_fraction(les):
    # construct a mapping of indices between openIFS levels and Dales height levels
    Zh = les.gcm_Zh  # half level heights. Ends with 0 for the ground.
    zh = les.zh.value_in(units.m)
    indices = numpy.searchsorted(zh, Zh, side="right")[:-1:][::-1]  # find indices in zh corresponding to Oifs levels
    # right: when heights are equal, return the largest index, discard last entry(ground=0) and reverse order
    A = les.get_cloudfraction(indices)[::-1]  # reverse order
    return A


gcm_vars = ["U", "V", "T", "SH", "QL", "QI", "Pfull", "Phalf", "A"]
surf_vars = ["Z0M", "Z0H", "QLflux", "QIflux", "SHflux", "TLflux", "TSflux"]
cpl_units = {"U": units.m / units.s,
             "V": units.m / units.s,
             "T": units.K,
             "Pfull": units.Pa,
             "Phalf": units.Pa,
             "Z0M": units.m,
             "Z0H": units.m,
             "QLflux": units.kg / units.m ** 2 / units.s,
             "QIflux": units.kg / units.m ** 2 / units.s,
             "SHflux": units.kg / units.m ** 2 / units.s,
             "TLflux": units.W / units.m ** 2,
             "TSflux": units.W / units.m ** 2}

var_to_netcdf_name = {"Z0M": "z0m",
                      "Z0H": "z0h",
                      "Phalf": "Ph",
                      "Pfull": "Pf"
                      }


# Retrieves all necessary vertical profiles to distribute to LES models:
def gather_gcm_data(gcm, les_models, couple_surface, output_column_indices=None):
    extra_cols = [] if output_column_indices is None else output_column_indices
    cols = [les.grid_index for les in les_models] + extra_cols
    start = time.time()
    profile_data = {}  # Contains all required profiles
    surface_data = {}  # Contains all required surface fields
    # Fill up vertical profiles...
    for gcm_var in gcm_vars:
        if not any(cols):
            profile_data[gcm_var] = []
        else:
            unit = cpl_units.get(gcm_var, None)
            data = gcm.get_profile_fields(gcm_var, cols)
            profile_data[gcm_var] = data.value_in(unit) if unit else data
    # Retrieve fluxes...
    if couple_surface:
        for surf_var in surf_vars:
            if not any(cols):
                surface_data[surf_var] = []
            else:
                unit = cpl_units.get(surf_var, None)
                data = gcm.get_surface_field(surf_var, cols)
                surface_data[surf_var] = data.value_in(unit) if unit else data

    walltime = time.time() - start
    log.info("Fetching gcm data took %d s" % walltime)

    # Attach vertical profiles to LES models:
    for i, les in enumerate(les_models):
        for varname in gcm_vars:
            setattr(les, varname, profile_data[varname][i][:])
        if couple_surface:
            for varname in surf_vars:
                setattr(les, varname, surface_data[varname][i])

    # Store data for the extra output columns in netCDF
    for i, col in enumerate(extra_cols):
        C = {}
        for varname in gcm_vars:
            # map variable name to netCDF variable name - if not in dict the name is the same
            cdfname = var_to_netcdf_name.get(varname, varname)
            C[cdfname] = profile_data[varname][i + len(les_models)][:]
        output_column_conversion(C)
        if couple_surface:
            for varname in surf_vars:
                cdfname = var_to_netcdf_name.get(varname, varname)
                C[cdfname] = surface_data[varname][i + len(les_models)]
            C['z0m'], C['z0h'], C['wthl'], C['wqt'] = convert_surface_fluxes(C)

        spio.write_netCDF_data(col, **C)
        # print('Storing extra column data', varname, C)


# Converts the OpenIFS surface fluxes to LES quantities
def convert_surface_fluxes(les):
    if type(les) != dict:
        Z0M, Z0H, QLflux, QIflux, SHflux, TLflux, TSflux = (getattr(les, varname, None) for varname in surf_vars)
        Ph = getattr(les, "Phalf", None)
        T = getattr(les, "T", None)
    else:
        # make it possible to pass a dictionary instead of a les object
        Z0M, Z0H, QLflux, QIflux, SHflux, TLflux, TSflux = (les.get(varname, None) for varname in surf_vars)
        # Ph = les.get("Ph",None)
        # T  = les.get("T",None)
        Ph = les["Ph"]  # want to know immediately (crash) if these are missing
        T = les['T']

#    log.info("convert_surface_fluxes:")
    # rho = les.rhobf[0]
    # instantaneous density at the surface :    
    #    rho = sputils.mair*1e-3 * Ph[-1] / (sputils.rd * T[-1])
    rho = Ph[-1] / (sputils.rd * T[-1])
    # note mair is g/mol, need kg/mol
    # note: rd is R/M - universal gas constant / Molar mass of dry air

#    log.info("  rho : %8e kg/m^3    Ts : %8e K" % (rho, T[-1]))

    wqt = - (QLflux + QIflux + SHflux) / rho

    wthl = - TSflux * sputils.iexner(Ph[-1]) / (sputils.cp * rho)  # only SENSIBLE heat

    # Signs: 
    # Dales: positive fluxes are upwards - into the atmosphere
    # OpenIFS: positive fluxes are downwards

    return Z0M, Z0H, wthl, wqt


# get the OpenIFS state and convert to LES quantities
def convert_profiles(les, write=True):
    U, V, T, SH, QL, QI, Pf, Ph, A = (getattr(les, varname, None) for varname in gcm_vars)

    # virtual temperature - used to get heights
    c = sputils.rv / sputils.rd - 1  # epsilon^(-1) -1  = 0.61
    Tv = T * (1 + c * SH - (QL + QI))
    # is it correct to include QI here?
    # like liquid water, ice contributes to the density but not (much) to pressure
    dP = Ph[1:] - Ph[:-1]  # dP - pressure difference over one cell
    dZ = sputils.rd * Tv / (sputils.grav * Pf) * dP  # dZ - height of one cell

    # sum up dZ to get Z at half-levels.
    # 0 is at the end of the list, therefore reverse lists before and after.
    Zh = numpy.cumsum(dZ[::-1])[::-1]

    Zh = numpy.append(Zh, 0)  # append a 0 for ground

    # height of full levels - simply average half levels (for now)
    # better: use full level pressure to calculate height?
    Zf = (Zh[1:] + Zh[:-1]) * .5

    les.gcm_Zf = Zf  # save height levels in the les object for re-use
    les.gcm_Zh = Zh

    # Convert from OpenIFS quantities to les
    # note - different from modtestbed - iexner multiplied with both terms
    # could include QI as well.
    thl_ = (T - (sputils.rlv * (QL + QI)) / sputils.cp) * sputils.iexner(Pf)
    qt_ = SH + QL + QI

    # interpolate to les' heights
    # quirks:
    #   Zf must be increasing, so reverse the gcm arrays
    #   outside the range of Zf, interp returns the first or the last point of the range

    h = les.zf.value_in(units.m)

    thl = numpy.interp(h, Zf[::-1], thl_[::-1])
    qt = numpy.interp(h, Zf[::-1], qt_[::-1])
    ql = numpy.interp(h, Zf[::-1], QL[::-1])
    u = numpy.interp(h, Zf[::-1], U[::-1])
    v = numpy.interp(h, Zf[::-1], V[::-1])

    if write:
        spio.write_les_data(les, U=U, V=V, T=T, SH=SH, QL=QL, QI=QI,
                            Pf=Pf, Ph=Ph[1:], Zf=Zf, Zh=Zh[1:],
                            Psurf=Ph[-1], Tv=Tv, THL=thl_, QT=qt_)

    return u, v, thl, qt, Ph[-1] | units.Pa, ql


# calculates QT and THL etc for GCM profiles for extra output columns
# like convert_profiles() for the les columns
def output_column_conversion(profile):
    c = sputils.rv / sputils.rd - 1  # epsilon^(-1) -1  = 0.61
    profile['Tv'] = profile['T'] * (1 + c * profile['SH'] - (profile['QL'] + profile['QI']))

    dP = profile['Ph'][1:] - profile['Ph'][:-1]  # dP - pressure difference over one cell
    dZ = sputils.rd * profile['Tv'] / (sputils.grav * profile['Pf']) * dP  # dZ - height of one cell

    # sum up dZ to get Z at half-levels.
    # 0 is at the end of the list, therefore reverse lists before and after.
    Zh = numpy.cumsum(dZ[::-1])[::-1]

    Zh = numpy.append(Zh, 0)  # append a 0 for ground

    # height of full levels - simply average half levels (for now)
    # better: use full level pressure to calculate height?
    # Zf = (Zh[1:] + Zh[:-1]) * .5
    profile['Zh'] = Zh[1:]
    profile['Psurf'] = profile['Ph'][-1]
    profile['Ph'] = profile['Ph'][1:]
    profile['THL'] = (profile['T'] - (sputils.rlv * (profile['QL'] + profile['QI'])) / sputils.cp) * sputils.iexner(profile['Pf'])
    profile['QT'] = profile['SH'] + profile['QL'] + profile['QI']


# set the dales state
# is u, v, thl, qt are vertical profiles, numpy broadcasting stretch them to 3D fields
# The values are randomly perturbed in the interval [-w,w]
# TODO check w with DALES input files

def set_les_state(les, u, v, thl, qt, ps=None):
    # tiny noise used until feb 2018
    # vabsmax,qabsmax = 0.001,0.00001
    # les.set_field('U',   numpy.random.uniform(-vabsmax, vabsmax, (les.itot, les.jtot, les.k)) + u)
    # les.set_field('V',   numpy.random.uniform(-vabsmax, vabsmax, (les.itot, les.jtot, les.k)) + v)
    # les.set_field('THL', numpy.random.uniform(-vabsmax, vabsmax, (les.itot, les.jtot, les.k)) + thl)
    # les.set_field('QT',  numpy.random.uniform(-qabsmax, qabsmax, (les.itot, les.jtot, les.k)) + qt)

    # more noise, according to Dales defaults. qabsmax defaults to 1e-5, 2.5e-5 is from a namoptions file
    vabsmax, thlabsmax, qabsmax = 0.5, 0.1, 2.5e-5
    les.set_field('U', numpy.random.uniform(-vabsmax, vabsmax, (les.itot, les.jtot, les.k)) + u)
    les.set_field('V', numpy.random.uniform(-vabsmax, vabsmax, (les.itot, les.jtot, les.k)) + v)
    les.set_field('THL', numpy.random.uniform(-thlabsmax, thlabsmax, (les.itot, les.jtot, les.k)) + thl)
    les.set_field('QT', numpy.random.uniform(-qabsmax, qabsmax, (les.itot, les.jtot, les.k)) + qt)

    if ps:
        les.set_surface_pressure(ps)


# Computes and applies the forcings to the les model before time stepping,
# relaxing it toward the gcm mean state.
def set_les_forcings(les, gcm, dt_gcm, factor, couple_surface, qt_forcing='sp'):
    u, v, thl, qt, ps, ql = convert_profiles(les)

    # get dales slab averages
    u_d = les.get_profile_U().value_in(units.m / units.s)
    v_d = les.get_profile_V().value_in(units.m / units.s)
    thl_d = les.get_profile_THL().value_in(units.K)
    qt_d = les.get_profile_QT()
    ql_d = les.get_profile_QL()
    ps_d = les.get_surface_pressure()
    # ft = dt  # forcing time constant

    # forcing
    f_u = factor * (u - u_d) / dt_gcm
    f_v = factor * (v - v_d) / dt_gcm
    f_thl = factor * (thl - thl_d) / dt_gcm
    f_qt = factor * (qt - qt_d) / dt_gcm
    f_ps = factor * (ps - ps_d) / dt_gcm
    f_ql = factor * (ql - ql_d) / dt_gcm

    # log.info("RMS forcings at %d during time step" % les.grid_index)
    # dt_gcm = gcm.get_timestep().value_in(units.s)
    # log.info("  u  : %f" % (sputils.rms(f_u)*dt_gcm))
    # log.info("  v  : %f" % (sputils.rms(f_v)*dt_gcm))
    # log.info("  thl: %f" % (sputils.rms(f_thl)*dt_gcm))
    # log.info("  qt : %f" % (sputils.rms(f_qt)*dt_gcm))

    # store forcings on dales in the statistics
    spio.write_les_data(les, f_u=f_u, f_v=f_v, f_thl=f_thl, f_qt=f_qt)

    # set tendencies for Dales
    les.set_tendency_U(f_u)
    les.set_tendency_V(f_v)
    les.set_tendency_THL(f_thl)
    les.set_tendency_QT(f_qt)
    les.set_tendency_surface_pressure(f_ps)
    les.set_tendency_QL(f_ql)                # used in experimental local qt nudging
    les.set_ref_profile_QL(ql)               # used in experimental variability nudging

    les.ql_ref = ql                          # store ql profile from GCM, interpolated to the LES levels
                                             # for another variant of variability nudging 
        
    # transfer surface quantities
    if couple_surface:
        z0m, z0h, wt, wq = convert_surface_fluxes(les)
        les.set_z0m_surf(z0m)
        les.set_z0h_surf(z0h)
        les.set_wt_surf(wt)
        les.set_wq_surf(wq)
        spio.write_les_data(les, z0m=z0m, z0h=z0h, wthl=wt, wqt=wq)
        spio.write_les_data(les, TLflux=les.TLflux, TSflux=les.TSflux,
                            SHflux=les.SHflux, QLflux=les.QLflux, QIflux=les.QIflux)

    if qt_forcing == 'variance':
        if les.get_model_time() > 0 | units.s:
            starttime = time.time()
            variability_nudge(les, gcm)
            walltime = time.time() - starttime
            log.info("variability nudge took %6.2f s"%walltime)


# Computes the LES tendencies upon the GCM:
def set_gcm_tendencies(gcm, les, factor=1):
    U, V, T, SH, QL, QI, Pf, Ph, A = (getattr(les, varname, None) for varname in gcm_vars)

    Zf = les.gcm_Zf  # note: gcm Zf varies in time and space - must get it again after every step, for every column
    h = les.zf.value_in(units.m)
    u_d = les.get_profile_U().value_in(units.m / units.s)
    v_d = les.get_profile_V().value_in(units.m / units.s)
    sp_d = les.get_presf().value_in(units.Pa)
    thl_d = les.get_profile_THL().value_in(units.K)
    qt_d = les.get_profile_QT()
    ql_d = les.get_profile_QL()
    ql_ice_d = les.get_profile_QL_ice()  # ql_ice is the ice part of QL
    ql_water_d = ql_d - ql_ice_d  # ql_water is the water part of ql
    qr_d = les.get_profile_QR()
    A_d = get_cloud_fraction(les)
    # dales state
    # dales.cdf.variables['presh'][gcm.step] = dales.get_presh().value_in(units.Pa) # todo associate with zh in netcdf

    # calculate real temperature from Dales' thl, qt, using the pressures from openIFS
    pf = numpy.interp(h, Zf[::-1], Pf[::-1])
    t = thl_d * sputils.exner(pf) + sputils.rlv * ql_d / sputils.cp

    # get real temperature from Dales - note it is calculated internally from thl and ql
    t_d = les.get_profile_T().value_in(units.K)

    spio.write_les_data(les, u=u_d, v=v_d, presf=sp_d, qt=qt_d, ql=ql_d,
                        ql_ice=ql_ice_d, ql_water=ql_water_d, thl=thl_d,
                        t=t, t_=t_d, qr=qr_d)

    # forcing
    ft = gcm.get_timestep().value_in(units.s)  # should be the length of the NEXT time step

    # interpolate to GCM heights
    t_d = numpy.interp(Zf, h, t_d)
    qt_d = numpy.interp(Zf, h, qt_d)
    ql_d = numpy.interp(Zf, h, ql_d)
    ql_water_d = numpy.interp(Zf, h, ql_water_d)
    ql_ice_d = numpy.interp(Zf, h, ql_ice_d)
    u_d = numpy.interp(Zf, h, u_d)
    v_d = numpy.interp(Zf, h, v_d)

    les_height = h[-1]
    # log.info("Height of LES system: %f" % les_height)
    i = 0
    for i in range(0, len(Zf)):
        if Zf[i] < les_height:
            break
    start_index = i  # first index in the openIFS column which is inside the Dales system
    # log.info("start_index: %d" % start_index)

    f_T = factor * (t_d - T) / ft
    f_SH = factor * ((qt_d - ql_d) - SH) / ft  # !!!!! -ql_d here - SH is vapour only.
    f_QL = factor * (ql_water_d - QL) / ft  # condensed liquid water
    f_QI = factor * (ql_ice_d - QI) / ft  # condensed water as ice
    # f_QL = factor * (ql_d - (QL+QI)) / ft dales QL is both liquid and ice - f_QL is liquid only. this conserves
    # water mass but makes an error in latent heat.
    f_U = factor * (u_d - U) / ft
    f_V = factor * (v_d - V) / ft
    f_A = factor * (A_d - A) / ft

    f_T[0:start_index] = 0  # zero out the forcings above the Dales system
    f_SH[0:start_index] = 0  # TODO : taper off smoothly instead
    f_QL[0:start_index] = 0
    f_QI[0:start_index] = 0
    f_U[0:start_index] = 0
    f_V[0:start_index] = 0
    f_A[0:start_index] = 0

    gcm.set_profile_tendency("U", les.grid_index, f_U)
    gcm.set_profile_tendency("V", les.grid_index, f_V)
    gcm.set_profile_tendency("T", les.grid_index, f_T)
    gcm.set_profile_tendency("SH", les.grid_index, f_SH)
    gcm.set_profile_tendency("QL", les.grid_index, f_QL)
    gcm.set_profile_tendency("QI", les.grid_index, f_QI)
    gcm.set_profile_tendency("A", les.grid_index, f_A)

    # store forcings on GCM in the statistics in the corresponding LES group
    spio.write_les_data(les, f_U=f_U, f_V=f_V, f_T=f_T, f_SH=f_SH, A=A, f_QL=f_QL, f_QI=f_QI)


def set_gcm_tendencies_from_file(gcm, les):
    t = gcm.get_model_time().value_in(units.s)
    ti = (numpy.abs(spio.cdf_root.variables['Time'] - t)).argmin()

    print('set_gcm_tendencies_from_file()', t, ti, spio.cdf_root.variables['Time'][ti])
    
    gcm.set_profile_tendency("U",  les.grid_index, les.cdf.variables['f_U' ][ti])
    gcm.set_profile_tendency("V",  les.grid_index, les.cdf.variables['f_V' ][ti])
    gcm.set_profile_tendency("T",  les.grid_index, les.cdf.variables['f_T' ][ti])
    gcm.set_profile_tendency("SH", les.grid_index, les.cdf.variables['f_SH'][ti])
    gcm.set_profile_tendency("QL", les.grid_index, les.cdf.variables['f_QL'][ti])
    gcm.set_profile_tendency("QI", les.grid_index, les.cdf.variables['f_QI'][ti])
    gcm.set_profile_tendency("A",  les.grid_index, les.cdf.variables['f_A' ][ti])


# fetch LES profiles and write to spifs.nc - used during spinup
def write_les_profiles(les):
    U, V, T, SH, QL, QI, Pf, Ph, A = (getattr(les, varname, None) for varname in gcm_vars)

    Zf = les.gcm_Zf  # note: gcm Zf varies in time and space - must get it again after every step, for every column
    h = les.zf.value_in(units.m)
    u_d = les.get_profile_U().value_in(units.m / units.s)
    v_d = les.get_profile_V().value_in(units.m / units.s)
    sp_d = les.get_presf().value_in(units.Pa)
    thl_d = les.get_profile_THL().value_in(units.K)
    qt_d = les.get_profile_QT()
    ql_d = les.get_profile_QL()
    ql_ice_d = les.get_profile_QL_ice()  # ql_ice is the ice part of QL
    ql_water_d = ql_d - ql_ice_d  # ql_water is the water part of ql
    qr_d = les.get_profile_QR()
    A_d = get_cloud_fraction(les)
    # dales state
    # dales.cdf.variables['presh'][gcm.step] = dales.get_presh().value_in(units.Pa) # todo associate with zh in netcdf

    # calculate real temperature from Dales' thl, qt, using the pressures from openIFS
    pf = numpy.interp(h, Zf[::-1], Pf[::-1])
    t = thl_d * sputils.exner(pf) + sputils.rlv * ql_d / sputils.cp

    # get real temperature from Dales - note it is calculated internally from thl and ql
    t_d = les.get_profile_T().value_in(units.K)

    spio.write_les_data(les, u=u_d, v=v_d, presf=sp_d, qt=qt_d, ql=ql_d,
                        ql_ice=ql_ice_d, ql_water=ql_water_d, thl=thl_d,
                        t=t, t_=t_d, qr=qr_d)


# TODO this routine sometimes hangs for a very long time, especially if it is called when
# variance nudging is not enabled in the LES
def variability_nudge(les, gcm):
    # this cannot be used before the LES has been stepped - otherwise qsat and ql are not defined.
    
    qsat = les.get_field("Qsat")
    qt = les.get_field("QT")
    ql2 = les.get_profile("QL")

    qt_av = les.get_profile("QT")
    
    ql = numpy.sum(numpy.maximum(qt - qsat, 0), axis=(0,1)) / (les.itot * les.jtot)

    # print('---', les.lat, les.lon, '---')
    # print(les.QL)
    # print(les.ql_ref)
    # print(ql)
    # print(ql2)
    # print(les.itot, les.jtot)
    # print ('---------')

    # get ql difference
    # note the implicit k, qt, qt_av, qsat variables
    def get_ql_diff(beta):
        return numpy.sum(numpy.maximum( beta*(qt[:,:,k]-qt_av[k]) + qt_av[k] - qsat[:,:,k], 0)) / (les.itot * les.jtot) - les.ql_ref[k]

    beta_min = 0 # search interval
    beta_max = 2000
    
    beta = numpy.ones(les.k)
    for k in range(0, les.k):
        current_ql_diff =  get_ql_diff(1)

        if les.ql_ref[k] > 1e-9:  # significant amount of clouds in the GCM. Nudge towards this amount.
            # print (k, 'significant ql_ref')
            q_min = get_ql_diff(beta_min)
            q_max = get_ql_diff(beta_max)
            if q_min > 0 or q_max < 0:
                log.info("k:%d didn't bracket a zero. qmin:%f, qmax:%f, qt_avg:%f, stdev(qt):%f "%
                         (k, q_min, q_max, numpy.mean(qt[:,:,k]), numpy.std(qt[:,:,k])))
                # seems to happen easily in the sponge layer, where the variability is kept small
                continue
            beta[k] = scipy.optimize.brentq(get_ql_diff, beta_min, beta_max)

        elif ql[k] > les.ql_ref[k]: # The GCM says no clouds, or very little, and the LES has more than this. 
                                    # Nudge towards barely unsaturated.
            i,j = numpy.unravel_index(numpy.argmax(qt[:,:,k] - qsat[:,:,k]), qt[:,:,k].shape)
            beta[k] = (qsat[i,j,k] - qt_av[k]) / (qt[i,j,k] - qt_av[k])
            log.info('%d nudging towards non-saturation. Max at (%d,%d). qt:%f, qsat:%f, qt_av[k]:%f, beta:%f, ql_avg:%f, ql_ref:%f'%
                     (k, i, j, qt[i,j,k], qsat[i,j,k], qt_av[k], beta[k], ql[k], les.ql_ref[k]))
            if beta[k] < 0:
                # this happens when qt_av > qsat
                log.info('  beta<0, setting beta=1 ')
                beta[k] = 1
        else:
            continue # no nudge - don't print anything
        
        # print (k, current_ql_diff, les.ql_ref[k], beta[k])
        
    alpha = numpy.log(beta) / gcm.get_timestep()
    les.set_qt_variability_factor(alpha)

    qt_std = numpy.std(qt, axis=(0,1)) 

    spio.write_les_data(les, qt_alpha=alpha.value_in(1/units.s))
    spio.write_les_data(les, qt_beta=beta, qt_std=qt_std)
                        
    
