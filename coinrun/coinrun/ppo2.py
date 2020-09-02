"""
This is a copy of PPO from openai/baselines (https://github.com/openai/baselines/blob/52255beda5f5c8760b0ae1f676aa656bb1a61f80/baselines/ppo2/ppo2.py) with some minor changes.
"""

import time
import joblib
import numpy as np
import tensorflow as tf
from collections import deque
import gym3

from mpi4py import MPI

from coinrun.tb_utils import TB_Writer
import coinrun.main_utils as utils

from coinrun.config import Config

mpi_print = utils.mpi_print

from baselines.common.runners import AbstractEnvRunner
from baselines.common.tf_util import initialize
from baselines.common.mpi_util import sync_from_root
from tensorflow.python.ops.ragged.ragged_util import repeat

from random import choice


def custom_loss(state_pair, env, actions_shape, a_n):
    """ Implementation of the custom loss, designed to measure the distance between two
    trajectories in the latent representation space of states within the policy network
    Args:
        state_pair: Tuple containing two Lists of byte strings containing the first (positive) &
            last (negative) states we wish to sample/contrast trajectories from
        env: current environment which is a dummy wrapper around a gym3 environment, from FakeEnv
            class which is defined in train_agent.py
        actions_shape: Tuple. Used to define the shape of the action array for one timestep for the 
            current environment
        a_n: Integer. Used to define a range [0, a_n] actions
            can take.
    Returns:
        anchors, pos_traj, neg_traj: numpy arrays containing the anchor, positive, and negative trajectories respectively
    """

    anchor = []
    pos_traj = []
    neg_traj = []
    pos_state, neg_state = state_pair

    # sample anchor
    env.callmethod("set_state", pos_state)
    for _ in range(Config.REP_LOSS_M):
        rand_a_1 = np.random.randint(a_n, size=actions_shape)
        obs_1, _, _, _ = env.step(rand_a_1)
        anchor.append(obs_1)
    
    # sample positive
    env.callmethod("set_state", pos_state)
    for _ in range(Config.REP_LOSS_M):
        rand_a_2 = np.random.randint(a_n, size=actions_shape)
        obs_2, _, _, _ = env.step(rand_a_2)
        pos_traj.append(obs_2)

    # sample negatives
    for _ in range(Config.NEGS):
        env.callmethod("set_state", neg_state)
        for _ in range(Config.REP_LOSS_M):
            rand_a_3 = np.random.randint(a_n, size=actions_shape)
            obs_3, _, _, _ = env.step(rand_a_3)
            neg_traj.append(obs_3)

    anchors = np.concatenate(anchor, axis=0)
    pos_traj = np.concatenate(pos_traj, axis=0)
    neg_traj = np.concatenate(neg_traj, axis=0)
    return anchors, pos_traj, neg_traj

class MpiAdamOptimizer(tf.train.AdamOptimizer):
    """Adam optimizer that averages gradients across mpi processes."""
    def __init__(self, comm, **kwargs):
        self.comm = comm
        self.train_frac = 1.0 - Config.get_test_frac()
        tf.train.AdamOptimizer.__init__(self, **kwargs)
    def compute_gradients(self, loss, var_list, **kwargs):
        grads_and_vars = tf.train.AdamOptimizer.compute_gradients(self, loss, var_list, **kwargs)

        grads_and_vars = [(g, v) for g, v in grads_and_vars if g is not None]
        flat_grad = tf.concat([tf.reshape(g, (-1,)) for g, v in grads_and_vars], axis=0)

        if Config.is_test_rank():
            flat_grad = tf.zeros_like(flat_grad)

        shapes = [v.shape.as_list() for g, v in grads_and_vars]
        sizes = [int(np.prod(s)) for s in shapes]

        num_tasks = self.comm.Get_size()
        buf = np.zeros(sum(sizes), np.float32)

        def _collect_grads(flat_grad):
            self.comm.Allreduce(flat_grad, buf, op=MPI.SUM)
            np.divide(buf, float(num_tasks) * self.train_frac, out=buf)
            return buf

        avg_flat_grad = tf.py_func(_collect_grads, [flat_grad], tf.float32)
        avg_flat_grad.set_shape(flat_grad.shape)
        avg_grads = tf.split(avg_flat_grad, sizes, axis=0)
        avg_grads_and_vars = [(tf.reshape(g, v.shape), v)
                    for g, (_, v) in zip(avg_grads, grads_and_vars)]

        return avg_grads_and_vars

class Model(object):
    def __init__(self, *, policy, ob_space, ac_space, nbatch_act, nbatch_train,
                nsteps, ent_coef, vf_coef, max_grad_norm):
        self.max_grad_norm = max_grad_norm

        sess = tf.get_default_session()

        train_model = policy(sess, ob_space, ac_space, nbatch_train, nsteps, max_grad_norm)
        norm_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        act_model = policy(sess, ob_space, ac_space, nbatch_act, 1, max_grad_norm)

        A = train_model.pdtype.sample_placeholder([None])
        ADV = tf.placeholder(tf.float32, [None])
        R = tf.placeholder(tf.float32, [None])
        OLDNEGLOGPAC = tf.placeholder(tf.float32, [None])
        OLDVPRED = tf.placeholder(tf.float32, [None])
        LR = tf.placeholder(tf.float32, [])
        CLIPRANGE = tf.placeholder(tf.float32, [])

        # VF loss
        vpred = train_model.vf_train  # Same as vf_run for SNI and default, but noisy for SNI2 while the boostrap is not
        vpredclipped = OLDVPRED + tf.clip_by_value(train_model.vf_train - OLDVPRED, - CLIPRANGE, CLIPRANGE)
        vf_losses1 = tf.square(vpred - R)
        vf_losses2 = tf.square(vpredclipped - R)
        vf_loss = .5 * tf.reduce_mean(tf.maximum(vf_losses1, vf_losses2))

        neglogpac_train = train_model.pd_train.neglogp(A)
        ratio_train = tf.exp(OLDNEGLOGPAC - neglogpac_train)
        pg_losses_train = -ADV * ratio_train
        pg_losses2_train = -ADV * tf.clip_by_value(ratio_train, 1.0 - CLIPRANGE, 1.0 + CLIPRANGE)
        pg_loss = tf.reduce_mean(tf.maximum(pg_losses_train, pg_losses2_train))
        approxkl_train = .5 * tf.reduce_mean(tf.square(neglogpac_train - OLDNEGLOGPAC))
        clipfrac_train = tf.reduce_mean(tf.to_float(tf.greater(tf.abs(ratio_train - 1.0), CLIPRANGE)))

        if Config.BETA >= 0:
            entropy = tf.reduce_mean(train_model.pd_train._components_distribution.entropy())
        else:
            entropy = tf.reduce_mean(train_model.pd_train.entropy())

        # Add entropy and policy loss for the samples as well
        if Config.SNI or Config.SNI2:
            neglogpac_run = train_model.pd_run.neglogp(A)
            ratio_run = tf.exp(OLDNEGLOGPAC - neglogpac_run)
            pg_losses_run = -ADV * ratio_run
            pg_losses2_run = -ADV * tf.clip_by_value(ratio_run, 1.0 - CLIPRANGE, 1.0 + CLIPRANGE)

            pg_loss += tf.reduce_mean(tf.maximum(pg_losses_run, pg_losses2_run))
            pg_loss /= 2.

            entropy += tf.reduce_mean(train_model.pd_run.entropy())
            entropy /= 2.

            approxkl_run = .5 * tf.reduce_mean(tf.square(neglogpac_run - OLDNEGLOGPAC))
            clipfrac_run = tf.reduce_mean(tf.to_float(tf.greater(tf.abs(ratio_run - 1.0), CLIPRANGE)))
        else:
            approxkl_run = tf.constant(0.)
            clipfrac_run = tf.constant(0.)

        params = tf.trainable_variables()
        weight_params = [v for v in params if '/b' not in v.name]

        total_num_params = 0

        for p in params:
            shape = p.get_shape().as_list()
            num_params = np.prod(shape)
            mpi_print('param', p, num_params)
            total_num_params += num_params

        mpi_print('total num params:', total_num_params)

        l2_loss = tf.reduce_sum([tf.nn.l2_loss(v) for v in weight_params])

        # The first occurance should be in the train_model

        if Config.BETA >= 0:
            info_loss = tf.get_collection(
                key="INFO_LOSS",
                scope="model/info_loss"
            )
            beta = Config.BETA

        elif Config.BETA_L2A >= 0:
            info_loss = tf.get_collection(
                key="INFO_LOSS_L2A",
                scope="model/info_loss"
            )
            beta = Config.BETA_L2A
        else:
            info_loss = [tf.constant(0.)]
            beta = 0

        print(info_loss)
        assert len(info_loss) == 1
        info_loss = info_loss[0]

        loss = pg_loss - entropy * ent_coef + vf_loss * vf_coef + l2_loss * Config.L2_WEIGHT + beta * info_loss

        if Config.SYNC_FROM_ROOT:
            trainer = MpiAdamOptimizer(MPI.COMM_WORLD, learning_rate=LR, epsilon=1e-5)
        else:
            trainer = tf.train.AdamOptimizer(learning_rate=LR, epsilon=1e-5)
        
        self.opt = trainer
        grads_and_var = trainer.compute_gradients(loss, params)

        grads, var = zip(*grads_and_var)
        if max_grad_norm is not None:
            grads, _grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        grads_and_var = list(zip(grads, var))

        _train = trainer.apply_gradients(grads_and_var)

        
        
        def train(lr, cliprange, obs, returns, masks, actions, values, neglogpacs, states=None):
            advs = returns - values
            adv_mean = np.mean(advs, axis=0, keepdims=True)
            adv_std = np.std(advs, axis=0, keepdims=True)
            advs = (advs - adv_mean) / (adv_std + 1e-8)

            td_map = {train_model.X:obs, A:actions, ADV:advs, R:returns, LR:lr,
                    CLIPRANGE:cliprange, OLDNEGLOGPAC:neglogpacs, OLDVPRED:values}
            if states is not None:
                td_map[train_model.S] = states
                td_map[train_model.M] = masks
            return sess.run(
                [pg_loss, vf_loss, entropy, approxkl_train, clipfrac_train, approxkl_run, clipfrac_run, l2_loss, info_loss, _train],
                td_map
            )[:-1]
            
        self.loss_names = ['policy_loss', 'value_loss', 'policy_entropy', 'approxkl_train', 'clipfrac_train', 'approxkl_run', 'clipfrac_run', 'l2_loss', 'info_loss_cv']

        def save(save_path):
            ps = sess.run(params)
            joblib.dump(ps, save_path)

        def load(load_path):
            loaded_params = joblib.load(load_path)
            restores = []
            for p, loaded_p in zip(params, loaded_params):
                restores.append(p.assign(loaded_p))
            sess.run(restores)

        self.train = train
        self.train_model = train_model
        self.act_model = act_model
        self.step = act_model.step
        self.value = act_model.value
        self.initial_state = act_model.initial_state
        self.save = save
        self.load = load
        self.custom_train = train_model.custom_train

        if Config.SYNC_FROM_ROOT:
            if MPI.COMM_WORLD.Get_rank() == 0:
                initialize()
            
            global_variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope="")
            sess.run(tf.global_variables_initializer())
            sync_from_root(sess, global_variables) #pylint: disable=E1101
        else:
            initialize()

class Runner(AbstractEnvRunner):
    def __init__(self, *, env, model, nsteps, gamma, lam):
        super().__init__(env=env, model=model, nsteps=nsteps)
        self.lam = lam
        self.gamma = gamma
        # List of two element tuples containing state lists for procgen,
        # where each tuple is the start & ending state for a trajectory.
        # The intuition here is that start and ending states will be very
        # different, giving us good positive/negative examples.
        self.diff_states = []

    def run(self, update_frac):
        # Here, we init the lists that will contain the mb of experiences
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones, mb_neglogpacs = [],[],[],[],[],[]
        mb_states = self.states
        epinfos = []
        first_state = self.env.callmethod("get_state")
        # For n in range number of steps
        for _ in range(self.nsteps):
            # Given observations, get action value and neglopacs
            # We already have self.obs because Runner superclass run self.obs[:] = env.reset() on init
            actions, values, self.states, neglogpacs = self.model.step(self.obs, update_frac, self.states, self.dones)
            mb_obs.append(self.obs.copy())
            mb_actions.append(actions)
            mb_values.append(values)
            mb_neglogpacs.append(neglogpacs)
            mb_dones.append(self.dones)

            # Take actions in env and look the results
            # Infos contains a ton of useful informations
            self.obs[:], rewards, self.dones, infos = self.env.step(actions)
            for info in infos:
                maybeepinfo = info.get('episode')
                if maybeepinfo: epinfos.append(maybeepinfo)
            mb_rewards.append(rewards)
        last_state = self.env.callmethod("get_state")
        # Add in first & last state from trajectory
        self.diff_states.append((first_state, last_state))
        # sample state pair
        state_pair = choice(self.diff_states)
        anchors, pos_traj, neg_traj = custom_loss(state_pair, self.env, actions.shape, self.env.action_space.n)
        
        # return environment to last state before sampling
        self.env.callmethod("set_state", last_state)
        #batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=self.obs.dtype)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
        mb_actions = np.asarray(mb_actions)
        mb_values = np.asarray(mb_values, dtype=np.float32)
        mb_neglogpacs = np.asarray(mb_neglogpacs, dtype=np.float32)
        mb_dones = np.asarray(mb_dones, dtype=np.bool)
        last_values = self.model.value(self.obs, update_frac, self.states, self.dones)
        # discount/bootstrap off value fn
        mb_returns = np.zeros_like(mb_rewards)
        mb_advs = np.zeros_like(mb_rewards)
        lastgaelam = 0
        for t in reversed(range(self.nsteps)):
            if t == self.nsteps - 1:
                nextnonterminal = 1.0 - self.dones
                nextvalues = last_values
            else:
                nextnonterminal = 1.0 - mb_dones[t+1]
                nextvalues = mb_values[t+1]
            delta = mb_rewards[t] + self.gamma * nextvalues * nextnonterminal - mb_values[t]
            mb_advs[t] = lastgaelam = delta + self.gamma * self.lam * nextnonterminal * lastgaelam
        mb_returns = mb_advs + mb_values

        return (*map(sf01, (mb_obs, mb_returns, mb_dones, mb_actions, mb_values, mb_neglogpacs)),
            mb_states, epinfos), anchors, pos_traj, neg_traj

def sf01(arr):
    """
    swap and then flatten axes 0 and 1
    """
    s = arr.shape
    return arr.swapaxes(0, 1).reshape(s[0] * s[1], *s[2:])


def constfn(val):
    def f(_):
        return val
    return f


def learn(*, policy, env, nsteps, total_timesteps, ent_coef, lr,
             vf_coef=0.5,  max_grad_norm=0.5, gamma=0.99, lam=0.95,
            log_interval=10, nminibatches=4, noptepochs=4, cliprange=0.2,
            save_interval=0, load_path=None):
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    mpi_size = comm.Get_size()

    sess = tf.get_default_session()

    if isinstance(lr, float): lr = constfn(lr)
    else: assert callable(lr)
    if isinstance(cliprange, float): cliprange = constfn(cliprange)
    else: assert callable(cliprange)
    total_timesteps = int(total_timesteps)

    nenvs = env.num_envs
    ob_space = env.observation_space
    ac_space = env.action_space
    nbatch = nenvs * nsteps
    
    nbatch_train = nbatch // nminibatches

    model = Model(policy=policy, ob_space=ob_space, ac_space=ac_space, nbatch_act=nenvs, nbatch_train=nbatch_train,
                    nsteps=nsteps, ent_coef=ent_coef, vf_coef=vf_coef,
                    max_grad_norm=max_grad_norm)

    utils.load_all_params(sess)

    runner = Runner(env=env, model=model, nsteps=nsteps, gamma=gamma, lam=lam)

    epinfobuf10 = deque(maxlen=10)
    epinfobuf100 = deque(maxlen=100)
    tfirststart = time.time()
    active_ep_buf = epinfobuf100

    nupdates = total_timesteps//nbatch
    mean_rewards = []
    datapoints = []

    run_t_total = 0
    train_t_total = 0

    can_save = True
    checkpoints = [32, 64]
    saved_key_checkpoints = [False] * len(checkpoints)

    if Config.SYNC_FROM_ROOT and rank != 0:
        can_save = False

    def save_model(base_name=None):
        base_dict = {'datapoints': datapoints}
        utils.save_params_in_scopes(sess, ['model'], Config.get_save_file(base_name=base_name), base_dict)

    # For logging purposes, allow restoring of update
    start_update = 0
    if Config.RESTORE_STEP is not None:
        start_update = Config.RESTORE_STEP // nbatch

    tb_writer = TB_Writer(sess)

    for update in range(start_update+1, nupdates+1):
        assert nbatch % nminibatches == 0
        nbatch_train = nbatch // nminibatches
        tstart = time.time()
        frac = 1.0 - (update - 1.0) / nupdates
        lrnow = lr(frac)
        cliprangenow = cliprange(frac)

        mpi_print('collecting rollouts...')
        run_tstart = time.time()

        packed, anchors, pos_traj, neg_traj = runner.run(update_frac=update/nupdates)
        obs, returns, masks, actions, values, neglogpacs, states, epinfos = packed
        epinfobuf10.extend(epinfos)
        epinfobuf100.extend(epinfos)

        run_elapsed = time.time() - run_tstart
        run_t_total += run_elapsed
        mpi_print('rollouts complete')

        mblossvals = []

        mpi_print('updating parameters...')
        train_tstart = time.time()

        mean_cust_loss = 0
        inds = np.arange(nbatch)
        for _ in range(noptepochs):
            np.random.shuffle(inds)
            for start in range(0, nbatch, nbatch_train):
                sess.run([model.train_model.train_dropout_assign_ops])
                end = start + nbatch_train
                mbinds = inds[start:end]
                slices = (arr[mbinds] for arr in (obs, returns, masks, actions, values, neglogpacs))
                mblossvals.append(model.train(lrnow, cliprangenow, *slices))
        if Config.CUSTOM_REP_LOSS:
            print('rep loss loop')
            mean_cust_loss = model.train_model.custom_train(anchors, pos_traj, neg_traj)[0]
        else:
            mean_cust_loss = 0
        # update the dropout mask
        sess.run([model.train_model.train_dropout_assign_ops])
        sess.run([model.train_model.run_dropout_assign_ops])

        train_elapsed = time.time() - train_tstart
        train_t_total += train_elapsed
        mpi_print('update complete')

        lossvals = np.mean(mblossvals, axis=0)
        tnow = time.time()
        fps = int(nbatch / (tnow - tstart))

        if update % log_interval == 0 or update == 1:
            step = update*nbatch
            rew_mean_10 = utils.process_ep_buf(active_ep_buf, tb_writer=tb_writer, suffix='', step=step)
            ep_len_mean = np.nanmean([epinfo['l'] for epinfo in active_ep_buf])
            
            mpi_print('\n----', update)

            mean_rewards.append(rew_mean_10)
            datapoints.append([step, rew_mean_10])
            tb_writer.log_scalar(ep_len_mean, 'ep_len_mean', step=step)
            tb_writer.log_scalar(fps, 'fps', step=step)
            tb_writer.log_scalar(mean_cust_loss, 'mean_custom_loss', step=step)

            mpi_print('time_elapsed', tnow - tfirststart, run_t_total, train_t_total)
            mpi_print('timesteps', update*nsteps, total_timesteps)

            mpi_print('eplenmean', ep_len_mean)
            mpi_print('eprew', rew_mean_10)
            mpi_print('fps', fps)
            mpi_print('total_timesteps', update*nbatch)
            mpi_print([epinfo['r'] for epinfo in epinfobuf10])

            if len(mblossvals):
                for (lossval, lossname) in zip(lossvals, model.loss_names):
                    mpi_print(lossname, lossval)
                    tb_writer.log_scalar(lossval, lossname, step=step)
            mpi_print('----\n')

        if can_save:
            if save_interval and (update % save_interval == 0):
                save_model()

            for j, checkpoint in enumerate(checkpoints):
                if (not saved_key_checkpoints[j]) and (step >= (checkpoint * 1e6)):
                    saved_key_checkpoints[j] = True
                    save_model(str(checkpoint) + 'M')

    save_model()

    env.close()
    return mean_rewards
