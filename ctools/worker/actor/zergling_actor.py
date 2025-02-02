import copy
import queue
import time
import uuid
from collections import namedtuple
from threading import Thread
from typing import List, Dict, Callable, Any, Tuple
from easydict import EasyDict
from collections import deque

import torch

from ctools.data import default_collate, default_decollate
from ctools.torch_utils import to_device, tensor_to_list
from ctools.utils import get_data_compressor, lists_to_dicts
from ctools.worker.agent import BaseAgent
from ctools.worker.actor import BaseActor
from ctools.worker.actor.env_manager import SubprocessEnvManager, BaseEnvManager


class ZerglingActor(BaseActor):
    """
    Feature:
      - one agent/sync many agents, many envs
      - async envs(step + reset)
      - batch network eval
      - different episode length env
      - periodic agent update
      - metadata + stepdata
    """

    # override
    def _init(self) -> None:
        super()._init()
        self._traj_queue = queue.Queue()
        self._result_queue = queue.Queue()
        self._update_agent_thread = Thread(target=self._update_agent, args=())
        self._update_agent_thread.daemon = True
        self._update_agent_thread.start()  # keep alive in the whole job
        self._model_deque = [deque(maxlen=1) for _ in range(2)]
        self._pack_trajectory_thread = Thread(target=self._pack_trajectory, args=())
        self._pack_trajectory_thread.daemon = True
        self._pack_trajectory_thread.start()
        self._send_result_thread = Thread(target=self._send_result_thread, args=())
        self._send_result_thread.daemon = True
        self._send_result_thread.start()  # keep alive in the whole job

    # override
    def _init_with_job(self, job: dict) -> None:
        super()._init_with_job(job)
        self._job = job
        self._logger.info('ACTOR({}): init with job {} in {}'.format(self._actor_uid, self._job['job_id'], time.time()))
        self._start_time = time.time()
        self._step_count = 0
        assert len(self._job['agent']) >= 1
        self._env_kwargs = self._cfg.actor.env_kwargs
        self._env_kwargs.env_cfg.player1.name = self._job['player_id'][0].split('_')[0]
        self._env_kwargs.env_cfg.player2.name = self._job['player_id'][1].split('_')[0]
        self._env_num = self._env_kwargs['env_num']
        self._compressor = get_data_compressor(self._cfg.actor.compressor)
        self._agent_update_freq = self._cfg.actor.agent_update_freq
        self._job_result = {k: [] for k in range(self._env_num)}
        self._collate_fn = default_collate
        self._decollate_fn = default_decollate
        self._env_manager = self._setup_env_manager()
        self._agent = self._setup_agent()
        self._obs_pool = {k: None for k in range(self._env_num)}
        self._act_pool = {k: None for k in range(self._env_num)}
        self._data_buffer = {k: [] for k in range(self._env_num)}
        self._last_data_buffer = {k: [] for k in range(self._env_num)}
        self._episode_result = {k: None for k in range(self._env_num)}
        self._job_finish_flag = False

    def _setup_env_manager(self) -> BaseEnvManager:
        env_cfg = EasyDict(self._env_kwargs['env_cfg'])
        map_name = env_cfg.map_name
        env_num = self._env_kwargs['env_num']
        if isinstance(env_cfg, dict):
            env_fn = self._setup_env_fn(env_cfg)
            env_cfg = [env_cfg for _ in range(env_num)]
        else:
            raise TypeError("not support env_cfg type: {}".format(env_cfg))
        env_manager = SubprocessEnvManager(
            env_fn=env_fn, env_cfg=env_cfg, env_num=env_num, episode_num=self._env_kwargs['episode_num'], map_name=map_name
        )
        env_manager.launch()
        return env_manager

    # override
    def _agent_inference(self, obs: Dict[int, Any]) -> Dict[int, Any]:
        # save in obs_pool
        for k, v in obs.items():
            self._obs_pool[k] = copy.deepcopy(v)

        env_id = obs.keys()
        obs = self._collate_fn(list(obs.values()))
        if self._cfg.actor.use_cuda:
            obs = to_device(obs, 'cuda')
        forward_kwargs = self._job['forward_kwargs']
        forward_kwargs['state_id'] = list(env_id)
        if len(self._job['agent']) == 1:
            data = self._agent.forward(obs, **forward_kwargs)
        else:
            data = [agent.forward(obs[i], **forward_kwargs) for i, agent in enumerate(self._agent)]
        if self._cfg.actor.use_cuda:
            data = to_device(data, 'cpu')
        data = self._decollate_fn(data)
        data = [lists_to_dicts(d) for d in data]
        data = {i: d for i, d in zip(env_id, data)}
        return data

    # override
    def _env_step(self, agent_output: Dict[int, Dict]) -> Dict[int, Any]:
        # save in act_pool
        for k, v in agent_output.items():
            self._act_pool[k] = copy.deepcopy(v)
        action = {k: v['action'] for k, v in agent_output.items()}
        return self._env_manager.step(action)

    # override
    def _process_timestep(self, timestep: Dict[int, namedtuple]) -> None:
        for env_id, t in timestep.items():
            data = self._get_transition(self._obs_pool[env_id], self._act_pool[env_id], timestep[env_id])
            self._data_buffer[env_id].append(data)
            self._step_count += 1
            if len(self._data_buffer[env_id]) == (self._adder_kwargs['data_push_length'] + 1):
                # last data copy must be in front of obs_next
                last = self._data_buffer[env_id][-1]
                data = self._data_buffer[env_id][:-1]
                self._last_data_buffer[env_id].clear()
                self._last_data_buffer[env_id] = copy.deepcopy(data)
                if self._adder_kwargs['use_gae']:
                    gamma = self._adder_kwargs['gamma']
                    gae_lambda = self._adder_kwargs['gae_lambda']
                    data = self._adder.get_gae(data, last['value'], gamma, gae_lambda)
                self._traj_queue.put({'data': data, 'env_id': env_id, 'agent_id': 0, 'job': copy.deepcopy(self._job)})
                self._data_buffer[env_id].clear()
                self._data_buffer[env_id].append(last)
            if t.done:
                self._job_result[env_id].append(t.info)
                self._logger.info('ACTOR({}): env{} finish episode in {}'.format(self._actor_uid, env_id, time.time()))
                cur_len = len(self._data_buffer[env_id])
                miss_len = self._adder_kwargs['data_push_length'] - cur_len
                data = self._last_data_buffer[env_id][-miss_len:] + self._data_buffer[env_id]
                if self._adder_kwargs['use_gae']:
                    gamma = self._adder_kwargs['gamma']
                    gae_lambda = self._adder_kwargs['gae_lambda']
                    data = self._adder.get_gae(data, torch.zeros(1), gamma, gae_lambda)
                self._traj_queue.put({'data': data, 'env_id': env_id, 'agent_id': 0, 'job': copy.deepcopy(self._job)})
                self._last_data_buffer[env_id].clear()
                self._data_buffer[env_id].clear()

    # ******************************** thread **************************************

    # override
    def _update_agent(self) -> None:
        last = time.time()
        while not self._end_flag:
            if hasattr(self, '_job') and hasattr(self, '_agent'):
                cur = time.time()
                interval = cur - last
                if interval < self._agent_update_freq:
                    time.sleep(self._agent_update_freq * 0.1)
                    continue
                else:
                    try:
                        for i in range(len(self._job['agent'])):
                            if i in self._job['update_agent']:
                                t = time.time()
                                agent_update_info, path = self.get_agent_update_info(self._job['learner_uid'][i])
                                self._model_deque[i].append(agent_update_info)
                                #if len(self._job['agent']) == 1:
                                #    self._agent.load_state_dict(agent_update_info)
                                #else:
                                #    self._agent[i].load_state_dict(agent_update_info)
                                self._logger.info(
                                    'ACTOR({}): update agent {} with {} in {}, cost_time:{}'.format(self._actor_uid, i, path, time.time(), time.time() - t)
                                )
                        last = time.time()
                    except:
                        
                        self._logger.error('update agent fail, try again!!!')
                        self._end_flag = True
            time.sleep(0.1)
        self._logger.info('update agent thread exit!!!!!!')

    # override
    def _pack_trajectory(self) -> None:
        def _pack(element):
            data, env_id, agent_id, job = list(element.values())
            # send metadata
            job_id = job['job_id']
            traj_id = "job_{}_env_{}_agent_{}_{}".format(job_id, env_id, agent_id, str(uuid.uuid1()))
            metadata = {
                'traj_id': traj_id,
                'learner_uid': job['learner_uid'][0],
                'launch_player': job['launch_player'],
                'env_id': env_id,
                'agent_id': agent_id,
                'actor_uid': self._actor_uid,
                'done': data[-1]['done'],
                # TODO(nyz) the relationship between traj priority and step priority
                'priority': 1.0,
                'traj_finish_time': time.time(),
                'job_id': job_id,
                'data_push_length': len(data),
                'compressor': self._cfg.actor.compressor,
                'job': job,
            }
            # save data
            data = self._compressor(data)
            t = time.time()
            self.send_traj_stepdata(traj_id, data)
            self.send_traj_metadata(metadata)
            self._logger.info('ACTOR({}): send traj({}) in {}, cost time:{}'.format(self._actor_uid, traj_id, time.time(), time.time() - t))

        finished_traj_num = 0
        while not self._end_flag:
            try:
                element = self._traj_queue.get(timeout=1)
            except queue.Empty:
                time.sleep(1)
                continue
            _pack(element)
            finished_traj_num += 1
            self._logger.info('ACTOR({}) finished {}'.format(self._actor_uid, finished_traj_num))
        self._logger.info('send traj thread exit!!!!!')

    def _send_result_thread(self):
        while not self._end_flag:
            try:
                result = self._result_queue.get(timeout=1)
            except queue.Empty:
                time.sleep(1)
                continue
            result_info = {
                'job_id': self._job['job_id'],
                'actor_uid': self._actor_uid,
                'player_id': self._job['player_id'],
                'launch_player': self._job['launch_player'],
                'result': result['result'],
                'dists': result['dists'],
                'units_num': result['units_num'],
            }
            self.send_result(result_info)
        self._logger.info('send result thread exit!!!!!!!')
        
    def _setup_env_fn(self, env_cfg: dict) -> Callable:
        """set env_fn"""
        raise NotImplementedError

    def _setup_agent(self) -> BaseAgent:
        """set agent, load init state_dict, reset"""
        raise NotImplementedError

    def _get_transition(self, obs: Any, agent_output: Dict, timestep: namedtuple) -> dict:
        """get one step transition"""
        raise NotImplementedError
