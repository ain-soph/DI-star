import copy
from collections import namedtuple, OrderedDict
from typing import Optional

import numpy as np
import torch
import torch.nn.functional as F

from ctools.pysc2.lib.static_data import BUILD_ORDER_REWARD_ACTIONS, UNIT_BUILD_ACTIONS, EFFECT_ACTIONS, RESEARCH_ACTIONS
from ctools.data.collate_fn import diff_shape_collate
from ctools.envs.common import EnvElement
from ctools.torch_utils import levenshtein_distance, hamming_distance, to_device


class AlphaStarReward(EnvElement):
    _name = 'AlphaStarReward'
    BattleValues = namedtuple('BattleValues', ['last_h', 'cur_h', 'last_a', 'cur_a'])

    # override
    def _init(self, agent_num, pseudo_reward_type: str, pseudo_reward_prob: float) -> None:
        self.agent_num = agent_num
        self.pseudo_reward_prob = pseudo_reward_prob
        self.pseudo_reward_type = pseudo_reward_type
        assert self.pseudo_reward_type in ['global', 'immediate']
        self.last_behaviour_z = None
        self.batch_size = self.agent_num
        self.device = torch.device('cpu')
        self.build_order_location_max_limit = 2
        self.build_order_location_rescale = 0.8
        self.battle_range = 5000
        self._reward_key = ['winloss', 'build_order', 'built_unit', 'upgrade', 'effect', 'battle']
        self._shape = {k: (1, ) for k in self._reward_key}
        begin_num = 20
        self._value = {
            'winloss': {
                'min': -1,
                'max': 1,
                'dtype': float,
                'dinfo': '-1, 0, 1'
            },
            'build_order': {
                'min': -begin_num,
                'max': 0,
                'dtype': float,
                'dinfo': 'float'
            },
            'built_unit': {
                'min': -len(UNIT_BUILD_ACTIONS),
                'max': 0,
                'dtype': float,
                'dinfo': 'int value'
            },
            'upgrade': {
                'min': -len(RESEARCH_ACTIONS),
                'max': 0,
                'dtype': float,
                'dinfo': 'int value'
            },
            'effect': {
                'min': -len(EFFECT_ACTIONS),
                'max': 0,
                'dtype': float,
                'dinfo': 'int value'
            },
            'battle': {
                'min': -self.battle_range,
                'max': self.battle_range,
                'dtype': float,
                'dinfo': 'float'
            }
        }
        self._to_agent_processor = self.get_pseudo_rewards
        self._from_agent_processor = None

    # override
    def _details(self) -> str:
        return '\t'.join(self._reward_key)

    def get_pseudo_rewards(
            self,
            rewards: list,
            action_types: list,
            episode_stats: list,
            loaded_eval_stats: list,
            game_loop: int,
            battle_values: 'AlphaStarReward.BattleValues',
            return_list: Optional[bool] = False
    ) -> dict:

        def check(t) -> bool:
            return isinstance(t, list) and len(t) == self.agent_num

        assert check(rewards) and check(action_types) and check(episode_stats) and check(loaded_eval_stats)
        assert isinstance(battle_values, self.BattleValues) or battle_values is None
        game_second = game_loop // 22
        # single player pseudo rewards

        ori_rewards = copy.deepcopy(rewards)
        behaviour_zs = []
        human_target_zs = []
        for i in range(self.agent_num):
            if self.pseudo_reward_type == 'global':
                behaviour_z = episode_stats[i].get_reward_z(use_max_bo_clip=True)
                # bo_length = len(behaviour_z['build_order']['type'])
                bo_length = 20
                human_target_z = loaded_eval_stats[i].get_reward_z_by_game_loop(
                    game_loop=None, build_order_length=bo_length
                )
            elif self.pseudo_reward_type == 'immediate':
                behaviour_z = episode_stats[i].get_reward_z(use_max_bo_clip=False)
                human_target_z = loaded_eval_stats[i].get_reward_z_by_game_loop(game_loop=game_loop)
            else:
                raise ValueError(f"{self.pseudo_reward_type} unknown!")
            behaviour_zs.append(behaviour_z)
            human_target_zs.append(human_target_z)
        game_seconds = [game_second] * self.agent_num
        behaviour_zs = diff_shape_collate(behaviour_zs)
        human_target_zs = diff_shape_collate(human_target_zs)
        masks = self._get_reward_masks(action_types, behaviour_zs, self.last_behaviour_z)

        rewards, dists = self._compute_pseudo_rewards(behaviour_zs, human_target_zs, rewards, game_seconds, masks)
        self.last_behaviour_z = copy.deepcopy(behaviour_zs)

        if loaded_eval_stats[0].excess_max_game_loop(game_loop):  # differnet agents have the same game_loop
            rewards = self._get_zero_rewards(ori_rewards)

        # multi players pseudo rewards
        if self.agent_num > 1:
            rewards = self._compute_battle_reward(rewards, battle_values)
        if return_list:
            rewards = [{k: rewards[k][i].unsqueeze(0) for k in rewards.keys()} for i in range(self.agent_num)]
            dists = [{k: dists[k][i].unsqueeze(0) for k in dists.keys()} for i in range(self.agent_num)]
        return rewards, dists

    def _get_zero_rewards(self, ori_rewards: list) -> dict:
        rewards = {}
        rewards['winloss'] = torch.FloatTensor(ori_rewards)
        for k in ['build_order', 'built_unit', 'upgrade', 'effect']:
            rewards[k] = torch.zeros(self.batch_size)
        rewards = to_device(rewards, self.device)
        return rewards

    def _get_reward_masks(self, action_type: list, behaviour_z: dict, last_behaviour_z: dict) -> dict:
        cum_stat_list = ['built_unit', 'effect', 'upgrade']
        action_map = {'built_unit': UNIT_BUILD_ACTIONS, 'effect': EFFECT_ACTIONS, 'upgrade': RESEARCH_ACTIONS}
        masks = {}
        mask_build_order = torch.zeros(self.batch_size)
        for i in range(self.batch_size):
            mask_build_order[i] = 1 if action_type[i] in BUILD_ORDER_REWARD_ACTIONS else 0
        masks['build_order'] = mask_build_order
        if last_behaviour_z is None:
            last_behaviour_z = {k: torch.zeros_like(v) for k, v in behaviour_z.items() if k in cum_stat_list}
            for k in cum_stat_list:
                mask = torch.zeros(self.batch_size)
                for i in range(self.batch_size):
                    if action_type[i] in action_map[k]:
                        mask[i] = 1
                masks[k] = mask
        else:
            for k in cum_stat_list:
                ne_num = behaviour_z[k].ne(last_behaviour_z[k]).sum(dim=1)
                masks[k] = torch.where(ne_num > 0, torch.ones(self.batch_size), torch.zeros(self.batch_size))
        masks = to_device(masks, self.device)
        return masks

    def _compute_pseudo_rewards(
            self, behaviour_z: dict, human_target_z: dict, rewards: list, game_seconds: int, masks: dict
    ) -> dict:
        """
            Overview: compute pseudo rewards from human replay z
            Arguments:
                - behaviour_z (:obj:`dict`)
                - human_target_z (:obj:`dict`)
                - rewards (:obj:`list`)
                - game_seconds (:obj:`int`)
                - masks (:obj:`dict`)
            Returns:
                - rewards (:obj:`dict`): a dict contains different type rewards
        """

        def loc_fn(p1, p2, max_limit=self.build_order_location_max_limit):
            p1 = p1.float().to(self.device)
            p2 = p2.float().to(self.device)
            dist = F.l1_loss(p1, p2, reduction='sum')
            dist = dist.clamp(0, max_limit)
            dist = dist / max_limit * self.build_order_location_rescale
            return dist.item()

        def get_time_factor(game_second):
            if game_second < 8 * 60:
                return 1.0
            elif game_second < 16 * 60:
                return 0.5
            elif game_second < 24 * 60:
                return 0.25
            else:
                return 0

        factors = torch.FloatTensor([get_time_factor(s) for s in game_seconds]).to(self.device)

        new_rewards = OrderedDict()
        new_rewards['winloss'] = torch.FloatTensor(rewards).to(self.device)
        # build_order
        p = np.random.uniform()
        build_order_reward = []
        dists = {'build_order': []}
        for i in range(self.batch_size):
            # only proper action can activate
            mask = masks['build_order'][i]
            # only some prob can activate
            mask = mask if p < self.pseudo_reward_prob else 0
            # if current the length of the behaviour_build_order is longer than that of human_target_z, return zero
            bo_dist = -levenshtein_distance(
                        behaviour_z['build_order']['type'][i][:20], human_target_z['build_order']['type'][i][:20],
                        behaviour_z['build_order']['loc'][i][:20], human_target_z['build_order']['loc'][i][:20], loc_fn
                    )
            dists['build_order'].append(-bo_dist)
            if (len(behaviour_z['build_order']['type'][i]) > 20
                    and self.pseudo_reward_type == 'global'):
                mask = 0
            build_order_reward.append(bo_dist * mask / 20)
        new_rewards['build_order'] = torch.FloatTensor(build_order_reward).to(self.device)
        dists['build_order'] = torch.FloatTensor(dists['build_order']).to(self.device)
        # built_unit, effect, upgrade
        # p is independent from all the pseudo reward and the same in a batch
        for k in ['built_unit', 'effect', 'upgrade']:
            mask = masks[k]
            p = np.random.uniform()
            mask_factor = 1 if p < self.pseudo_reward_prob else 0
            mask *= mask_factor
            hamming_dist = -hamming_distance(behaviour_z[k], human_target_z[k], factors)
            new_rewards[k] = hamming_dist * mask
            hamming_dist = -hamming_distance(behaviour_z[k], human_target_z[k])
            dists[k] = -hamming_dist
        for k in new_rewards.keys():
            new_rewards[k] = new_rewards[k].float()
        for k in dists.keys():
            dists[k] = dists[k].float()
        return new_rewards, dists

    def _compute_battle_reward(self, rewards: dict, battle_values: 'AlphaStarReward.BattleValues') -> dict:
        last_h, cur_h, last_a, cur_a = battle_values
        v = (cur_h - last_h) - (cur_a - last_a)
        v = torch.FloatTensor([v]).to(self.device)
        rewards['battle'] = torch.cat([v, -v])
        return rewards
