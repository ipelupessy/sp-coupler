#!/bin/bash

# This is a simple spifs run which fits on a single PC.
# OpenIFS with the T21 grid and 2 LES instances close to Barbados.
# This version uses the Amuse MPI channel.
# Demands an MPI implementation that supports comm_spawn.
# (Normally they do, Cray doesn't)


N_GCM=1
N_LES=2
LES_PROCS=1

OIFSDIR=oifs-input
DALESDIR=dales-input
OUT=output


mpiexec -n 1 python2 ./spmaster.py --steps 5  \
--poly        20 -50  10 -50   10 -40   20 -40	\
--gcmprocs $N_GCM --numles $N_LES --lesprocs $LES_PROCS  \
--gcmdir=$OIFSDIR --gcmexp=TEST \
--lesdir=$DALESDIR --odir=$OUT --cplsurf \
--channel=mpi 


# --spinup 14400 --spinup_steps 16





