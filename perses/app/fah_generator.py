__author__ = 'dominic rufa'

"""
Folding@Home perses executor

# prereqs
conda config --add channels omnia --add channels conda-forge
conda create -n perses python3.7 perses tqdm dicttoxml
pip uninstall --yes openmmtools
pip install git+https://github.com/choderalab/openmmtools.git

argv[1]: setup.yaml (argument for perses.app.setup_relative_calculation.getSetupOptions)
"""
import numpy as np
import os
import simtk.unit as unit
from simtk import openmm
import logging
_logger = logging.getLogger()
_logger.setLevel(logging.INFO)


#let's make a default lambda protocol
x = 'lambda'
# TODO change this for perses.annihilation.LambdaProtocol.default_functions
DEFAULT_ALCHEMICAL_FUNCTIONS = {
                             'lambda_sterics_core': x,
                             'lambda_electrostatics_core': x,
                             'lambda_sterics_insert': f"select(step({x} - 0.5), 1.0, 2.0 * {x})",
                             'lambda_sterics_delete': f"select(step({x} - 0.5), 2.0 * ({x} - 0.5), 0.0)",
                             'lambda_electrostatics_insert': f"select(step({x} - 0.5), 2.0 * ({x} - 0.5), 0.0)",
                             'lambda_electrostatics_delete': f"select(step({x} - 0.5), 1.0, 2.0 * {x})",
                             'lambda_bonds': x,
                             'lambda_angles': x,
                             'lambda_torsions': x}


def make_neq_integrator(nsteps_eq=250000, nsteps_neq=250000, neq_splitting='V R H O R V', timestep=4.0 * unit.femtosecond, alchemical_functions=DEFAULT_ALCHEMICAL_FUNCTIONS, **kwargs):
    """
    generate an openmmtools.integrators.PeriodicNonequilibriumIntegrator

    arguments
        nsteps_eq : int, default=250000
            Number of equilibration steps to dwell within lambda = 0 or 1 when reached
        nsteps_neq : int, default=250000
            Number of nonequilibrium switching steps for 0->1 and 1->0 switches
        neq_splitting : str, default='V R H O R V'
            Sequence of "R", "V", "O" (and optionally "{", "}", "V0", "V1", ...) substeps to be executed each timestep.
            "H" increments the global parameter `lambda` by 1/nsteps_neq for each step and accumulates protocol work.
        timestep : int, default=4.0 * unit.femtosecond
            integrator timestep
        alchemical_functions=dict
            dictionary containing alchemical functions of how to perturb each group. See DEFAULT_ALCHEMICAL_FUNCTIONS for example
        **kwargs :
            miscellaneous arguments for openmmtools.integrators.LangevinIntegrator

    returns
        integrator : openmmtools.integrators.PeriodicNonequilibriumIntegrator

    """
    from openmmtools.integrators import PeriodicNonequilibriumIntegrator
    integrator = PeriodicNonequilibriumIntegrator(alchemical_functions, nsteps_eq, nsteps_neq, neq_splitting, timestep=timestep)
    return integrator

def relax_structure(temperature, system, positions, nequil=1000, n_steps_per_iteration=250,platform_name='OpenCL',timestep=2.*unit.femtosecond,collision_rate=90./unit.picosecond):
    """
    arguments
        temperature : simtk.unit.Quantity with units compatible with kelvin
            temperature of simulation
        system : openmm.System
            system object for simulation
        positions : simtk.unit.Quantity of shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        nequil : int, default = 1000
            number of equilibration applications
        n_steps_per_iteration : int, default = 250
            numper of steps per nequil
        platform name : str default='OpenCL'
            platform to run openmm on. OpenCL is best as this is what is used on FAH
        timestep : simtk.unit.Quantity, default = 2*unit.femtosecond
            timestep for equilibration NOT for production
        collision_rate : simtk.unit.Quantity, default=90./unit.picosecond

    return
        state : openmm.State
            state of simulation (getEnergy=True, getForces=True, getPositions=True, getVelocities=True, getParameters=True)
    """

    from openmmtools.integrators import LangevinIntegrator
    _logger.info(f'Starting to relax')
    integrator = LangevinIntegrator(temperature=temperature, timestep=timestep, collision_rate=collision_rate)
    platform = openmm.Platform.getPlatformByName(platform_name)

    # prepare the plaform
    if platform_name in ['CUDA', 'OpenCL']:
        platform.setPropertyDefaultValue('Precision', 'mixed')
    if platform_name in ['CUDA']:
        platform.setPropertyDefaultValue('DeterministicForces', 'true')
    context = openmm.Context(system, integrator, platform)
    context.setPeriodicBoxVectors(*system.getDefaultPeriodicBoxVectors())
    context.setPositions(positions)

    _logger.info(f'Starting to minimise')
    openmm.LocalEnergyMinimizer.minimize(context)

    # Equilibrate
    _logger.info(f'set velocities to temperature')
    context.setVelocitiesToTemperature(temperature)
    _logger.info(f'Starting to equilibrate for {nequil*n_steps_per_iteration*timestep}')
    integrator.step(nequil*n_steps_per_iteration)
    context.setVelocitiesToTemperature(temperature)
    state = context.getState(getEnergy=True, getForces=True, getPositions=True, getVelocities=True, getParameters=True)
    _logger.info(f'Relax done')

    del context, integrator
    return state

def run_neq_fah_setup(ligand_file,
                      old_ligand_index,
                      new_ligand_index,
                      forcefield_files,
                      trajectory_directory,
                      complex_box_dimensions=(9.8, 9.8, 9.8),
                      solvent_box_dimensions=(3.5,3.5,3.5),
                      timestep=4.0 * unit.femtosecond,
                      eq_splitting = 'V R O R V',
                      neq_splitting='V R H O R V',
                      measure_shadow_work=False,
                      pressure=1.0,
                      temperature=300,
                      solvent_padding=9*unit.angstroms,
                      phases=['complex','solvent','vacuum'],
                      protein_pdb=None,
                      receptor_mol2=None,
                      small_molecule_forcefield = 'openff-1.0.0',
                      small_molecule_parameters_cache = None,
                      atom_expression=['IntType'],
                      bond_expression=['DefaultBonds'],
                      spectators=None,
                      neglect_angles=False,
                      anneal_14s=False,
                      nonbonded_method='PME',
                      map_strength=None,
                      softcore_v2=False,
                      save_setup_pickle_as=None,
                      render_atom_map=False,
                      alchemical_functions=DEFAULT_ALCHEMICAL_FUNCTIONS,
                      num_equilibration_iterations=1000,
                      num_equilibration_steps_per_iteration=250,
                      nsteps_eq=250000,
                      nsteps_neq=250000,
                      fe_type='fah',
                      collision_rate=1./unit.picoseconds,
                      collision_rate_setup=90./unit.picoseconds,
                      constraint_tolerance=1e-6,
                      **kwargs):
    """
    main execution function that will:
        - create a directory for each phase according to the `trajectory_directory` argument
        - make a subdirectory named f"RUN_{old_ligand_index}_{new_ligand_index}" given the specified ligand indices of the `ligand_file`
        - create topology proposals for all phases
        - create/serialize hybrid factories or all phases (and validate endstates)
        - create/serialize an openmmtools.integrators.PeriodicNonequilibriumIntegrator for all phases
        - relax generated structures with a minimizer and LangevinIntegrator for all phases
        - create/serialize a state associated with the relaxed structures
        - create/serialize a `core.xml` object for all phases

    arguments
        ligand_file : str
            .sdf (or any openeye-readable) file containing ligand labeled indices and structures
        old_ligand_index : int
            index of the old ligand
        new_ligand_index : int
            inded of the new ligand
        forcefield_files : list of str
            list of forcefields to use for complex/solvent parameterization
        trajectory_directory : str
            RUNXXX for FAH deployment
        complex_box_dimensions : Vec3, default=(9.8, 9.8, 9.8)
            define box dimensions of complex phase
        solvent_box_dimensions : Vec3, default=(3.5, 3.5, 3.5)
            define box dimensions of solvent phase
        timestep : simtk.unit.Quantity, default=4.*unit.femtosecond
            step size of nonequilibrium integration
        eq_splitting : str, default = 'V R O R V'
            splitting string of relaxation dynamics
        neq_splitting : str, default = 'V R H O R V'
            splitting string of nonequilibrium dynamics
        measure_shadow_work : bool, default=False
            True/False to measure shadow work
        pressure: float, default=1.
            pressure in atms for simulation
        temperature: float, default=300.,
            temperature in K for simulation
        phases: list, default = ['complex','solvent','vacuum']
            phases to run, where allowed phases are 'complex','solvent','vacuum'
        protein_pdb : str, default=None
            name of protein file
        receptor_mol2 : str, default=None
            name of receptor file if protein_pdb not provided
        small_molecule_forcefield : str, default='openff-1.0.0'
            small molecule forcefield filename
        small_molecule_parameters_cache : str, default=None
            cache file containing small molecule forcefield files
        atom_expression : list default=['IntType']
            list of string for atom mapping criteria. see oechem.OEExprOpts for options
        bond_expression : list default=['DefaultBonds']
            list of string for bond mapping criteria. see oechem.OEExprOpts for options
        map_strength : 'str', default=None
            atom and bond expressions will be ignored, and either a 'weak', 'default' or 'strong' map_strength will be used.
        spectators : str, default=None
            path to any non-alchemical atoms in simulation
        neglect_angles : bool, default=False
            wether to use angle terms in building of unique-new groups. False is strongly recommended
        anneal_14s : bool, default False
            Whether to anneal 1,4 interactions over the protocol;
        nonbonded_method : str, default='PME'
            nonbonded method to use
        softcore_v2=bool, default=False
            wether to use v2 softcore
        alchemical_functions : dict, default=DEFAULT_ALCHEMICAL_FUNCTIONS
            alchemical functions for transformation
        num_equilibration_iterations: int, default=1000
            number of equilibration steps to do during set up
        num_equilibration_steps_per_iteration: int, default=250,
            number of steps per iteration. default is 250 steps of 2fs, 1000 times which is 500ps of equilibration for SETUP
        nsteps_eq : int, default=250000
            number of normal MD steps to take for FAH integrator for PRODUCTION
        nsteps_neq : int, default=250000
            number of nonequilibrium steps to take for FAH integrator for PRODUCTION
        fe_type : str, default='fah'
            tells setup_relative_calculation() to use the fah pipeline
        collision_rate : simtk.unit.Quantity, default=1./unit.picosecond
            collision_rate for PRODUCTION
        collision_rate_setup : simtk.unit.Quantity, default=90./unit.picosecond
        constraint_tolerance : float, default=1e-6
            tolerance to use for constraints
    """
    from perses.app.setup_relative_calculation import run_setup
    from perses.utils import data
    #turn all of the args into a dict for passing to run_setup
    setup_options = locals()
    if 'kwargs' in setup_options.keys():
        setup_options.update(setup_options['kwargs'])

    #some modification for fah-specific functionality:
    setup_options['trajectory_prefix']=None
    setup_options['anneal_1,4s'] = False
    from perses.utils.openeye import generate_expression
    setup_options['atom_expr'] = generate_expression(setup_options['atom_expression'])
    setup_options['bond_expr'] = generate_expression(setup_options['bond_expression'])

    #run the run_setup to generate topology proposals and htfs
    _logger.info(f"spectators: {setup_options['spectators']}")
    setup_dict = run_setup(setup_options, serialize_systems=False, build_samplers=False)
    topology_proposals = setup_dict['topology_proposals']
    htfs = setup_dict['hybrid_topology_factories']

    #create solvent and complex directories
    for phase in htfs.keys():
        _logger.info(f'PHASE RUNNING: {phase}')
        _logger.info(f'Setting up phase {phase}')
        if phase == 'solvent':
            phase_dir = f"{setup_options['solvent_projid']}/RUNS"
        if phase == 'complex':
            phase_dir = f"{setup_options['complex_projid']}/RUNS"
        if phase == 'vacuum':
            phase_dir = 'VACUUM/RUNS'
        dir = os.path.join(os.getcwd(), phase_dir, trajectory_directory)
        if not os.path.exists(dir):
            os.mkdir(dir)

        np.savez_compressed(f'{dir}/htf',htfs[phase])

        #serialize the hybrid_system
        data.serialize(htfs[phase].hybrid_system, f"{dir}/system.xml")

        #make and serialize an integrator
        integrator = make_neq_integrator(**setup_options)
        data.serialize(integrator, f"{dir}/integrator.xml")

        #create and serialize a state
        try:
            state = relax_structure(temperature=temperature,
                            system = htfs[phase].hybrid_system,
                            positions = htfs[phase].hybrid_positions,
                            nequil = num_equilibration_iterations,
                            n_steps_per_iteration=num_equilibration_steps_per_iteration, collision_rate=collision_rate_setup)

            data.serialize(state, f"{dir}/state.xml")
        except Exception as e:
            print(e)
            passed=False
        else:
            passed=True

        pos = state.getPositions(asNumpy=True)
        pos = np.asarray(pos)

        import mdtraj as md
        top = htfs[phase].hybrid_topology
        np.save(f'{dir}/hybrid_topology', top)
        traj = md.Trajectory(pos, top)
        traj.remove_solvent(exclude=['CL','NA'],inplace=True)
        traj.save(f'{dir}/hybrid_{phase}.pdb')

        #lastly, make a core.xml
        nsteps_per_cycle = 2*nsteps_eq + 2*nsteps_neq
        ncycles = 1
        nsteps_per_ps = 250
        core_parameters = {
            'numSteps' : ncycles * nsteps_per_cycle,
            'xtcFreq' : 1000*nsteps_per_ps, # once per ns
            'xtcAtoms' : 'solute',
            'precision' : 'mixed',
            'globalVarFilename' : 'globals.csv',
            'globalVarFreq' : nsteps_per_ps,
        }
        # Serialize core.xml
        import dicttoxml
        with open(f'{dir}/core.xml', 'wt') as outfile:
            #core_parameters = create_core_parameters(phase)
            xml = dicttoxml.dicttoxml(core_parameters, custom_root='config', attr_type=False)
            from xml.dom.minidom import parseString
            dom = parseString(xml)
            outfile.write(dom.toprettyxml())

        #create a logger for reference
        references = {'start_ligand': old_ligand_index,
                      'end_ligand': new_ligand_index,
                      'protein_pdb': protein_pdb,
                      'passed_strucutre_relax': passed}

        np.save(f'{dir}/references',references)

        tp = topology_proposals
        from perses.utils.smallmolecules import render_atom_mapping
        render_atom_mapping(f'{dir}/atom_map.png', tp['ligand_oemol_old'], tp['ligand_oemol_new'], tp['non_offset_new_to_old_atom_map'])


def run(yaml_filename=None):
    import sys
    if yaml_filename is None:
        try:
            yaml_filename = sys.argv[1]
            _logger.info(f"Detected yaml file: {yaml_filename}")
        except IndexError as e:
            _logger.critical(f"You must specify the setup yaml file as an  argument to the script.")
    from perses.app.setup_relative_calculation import getSetupOptions
    import yaml
    yaml_file = open(yaml_filename, 'r')
    setup_options = yaml.load(yaml_file, Loader=yaml.FullLoader)
    yaml_file.close()

    import os
    # make master directories
    if not os.path.exists(f"{setup_options['complex_projid']}"):
        os.makedirs(f"{setup_options['complex_projid']}/RUNS/")
    if not os.path.exists(f"{setup_options['solvent_projid']}"):
        os.makedirs(f"{setup_options['solvent_projid']}/RUNS/")
    if not os.path.exists('VACUUM'):
        os.makedirs('VACUUM/RUNS/')

    # make run directories
    os.makedirs(f"{setup_options['complex_projid']}/RUNS/{setup_options['trajectory_directory']}")
    os.makedirs(f"{setup_options['solvent_projid']}/RUNS/{setup_options['trajectory_directory']}")
    os.makedirs(f"VACUUM/RUNS/{setup_options['trajectory_directory']}")

    run_neq_fah_setup(**setup_options)
