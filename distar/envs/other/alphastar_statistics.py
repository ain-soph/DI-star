import copy
import os
import logging

import numpy as np
import torch

from ctools.pysc2.lib.action_dict import GENERAL_ACTION_INFO_MASK
from ctools.pysc2.lib.static_data import NUM_BEGIN_ACTIONS, NUM_UNIT_BUILD_ACTIONS, NUM_EFFECT_ACTIONS, NUM_RESEARCH_ACTIONS, \
    UNIT_BUILD_ACTIONS_REORDER_ARRAY, EFFECT_ACTIONS_REORDER_ARRAY, RESEARCH_ACTIONS_REORDER_ARRAY, \
    BEGIN_ACTIONS_REORDER_ARRAY, BEGIN_ACTIONS, \
    OLD_BEGIN_ACTIONS_REORDER_INV
from ctools.envs.common import reorder_one_hot_array, batch_binary_encode, div_one_hot
from ..obs.alphastar_obs import LOCATION_BIT_NUM
from ctools.torch_utils import to_dtype, one_hot


def binary_search(data, item):
    if len(data) <= 0:
        raise RuntimeError("empty data with len: {}".format(len(data)))
    low = 0
    high = len(data) - 1
    while low <= high:
        mid = (high + low) // 2
        if data[mid] == item:
            return mid
        elif data[mid] < item:
            low = mid + 1
        else:
            high = mid - 1
    if low == len(data):
        low -= 1  # limit low within [0, len(data)-1]
    return low


class RealTimeStatistics:
    """
    Overview: real time agent statistics
    """

    def __init__(self, begin_num=20):
        self.action_statistics = {}
        self.cumulative_statistics = {}
        self.cumulative_statistics_game_loop = []
        self.begin_statistics = []
        self.begin_num = begin_num

    def update_action_stat(self, act, obs):
        # this will not clear the cache

        def get_unit_types(units, entity_type_dict):
            unit_types = set()
            for u in units:
                try:
                    unit_type = entity_type_dict[u]
                    unit_types.add(unit_type)
                except KeyError:
                    logging.warning("Not found unit(id: {})".format(u))
            return unit_types

        action_type = act.action_type
        if action_type not in self.action_statistics.keys():
            self.action_statistics[action_type] = {
                'count': 0,
                'selected_type': set(),
                'target_type': set(),
            }
        self.action_statistics[action_type]['count'] += 1
        entity_type_dict = {id: type for id, type in zip(obs['entity_raw']['id'], obs['entity_raw']['type'])}
        if act.selected_units is not None:
            units = act.selected_units
            unit_types = get_unit_types(units, entity_type_dict)
            self.action_statistics[action_type]['selected_type'] = \
                self.action_statistics[action_type]['selected_type'].union(unit_types)
        if act.target_units is not None:
            units = act.target_units
            unit_types = get_unit_types(units, entity_type_dict)
            self.action_statistics[action_type]['target_type'] = self.action_statistics[action_type][
                'target_type'].union(unit_types)

    def update_cum_stat(self, act, game_loop):
        # this will not clear the cache
        action_type = act.action_type
        goal = GENERAL_ACTION_INFO_MASK[action_type]['goal']
        if goal != 'other':
            if action_type not in self.cumulative_statistics.keys():
                self.cumulative_statistics[action_type] = {'count': 1, 'goal': goal}
            else:
                self.cumulative_statistics[action_type]['count'] += 1
            loop_stat = copy.deepcopy(self.cumulative_statistics)
            loop_stat['game_loop'] = game_loop
            self.cumulative_statistics_game_loop.append(loop_stat)

    def update_build_order_stat(self, act, game_loop, original_location):
        # this will not clear the cache
        worker_and_supply_units = (35, 64, 520, 222, 515, 503)
        action_type = act.action_type
        if action_type in worker_and_supply_units:  # exclude worker and supply
            return
        goal = GENERAL_ACTION_INFO_MASK[action_type]['goal']
        if action_type in BEGIN_ACTIONS:
            if goal == 'build':
                if original_location is not None:
                    location = original_location
                else:
                    location = act.target_location
                if isinstance(location, torch.Tensor):  # for build ves, no target_location
                    location = location.tolist()
            else:
                location = None
            self.begin_statistics.append({'action_type': action_type, 'location': location, 'game_loop': game_loop})

    def update_stat(self, act, obs, game_loop, original_location=None):
        """
        Update action_stat cum_stat and build_order_stat

        Args:
            act: Processed general action
            obs: observation
            game_loop: current game loop
        """
        if obs is not None:
            self.update_action_stat(act, obs)
        self.update_cum_stat(act, game_loop)
        self.update_build_order_stat(act, game_loop, original_location)

    def get_reward_z(self, use_max_bo_clip):
        """
        use_max_bo_clip (boolean): Whether to keep only the building orders of the first self.begin_num units.
        """
        beginning_build_order = self.begin_statistics
        if use_max_bo_clip and len(beginning_build_order) > self.begin_num:
            beginning_build_order = beginning_build_order[:self.begin_num+1]
        cumulative_stat = self.cumulative_statistics
        cum_stat_tensor = transform_cum_stat(cumulative_stat)
        ret = {
            'built_unit': cum_stat_tensor['unit_build'],
            'effect': cum_stat_tensor['effect'],
            'upgrade': cum_stat_tensor['research'],
            'build_order': transform_build_order_to_z_format(beginning_build_order),
        }
        ret = to_dtype(ret, torch.long)
        return ret

    def get_input_z(self, bo_length=20):
        ret = {
            'beginning_build_order': transform_build_order_to_input_format(self.begin_statistics, bo_length),
            'cumulative_stat': transform_cum_stat(self.cumulative_statistics)
        }
        return ret

    def get_stat(self):
        ret = {'begin_statistics': self.begin_statistics, 'cumulative_statistics': self.cumulative_statistics}
        return ret

    def get_norm_units_num(self):
        worker_and_supply_units = (35, 64, 520, 222, 515, 503)
        zerg_units = (498, 501, 507, 508, 514, 516, 519, 522, 524, 526, 528, 383, 396, 391, 400)
        units_num = {GENERAL_ACTION_INFO_MASK[k]['name'].split('_')[1]: 0 for k in zerg_units}
        max_num = 1
        for k in self.cumulative_statistics.keys():
            if k not in worker_and_supply_units and k in zerg_units \
                    and self.cumulative_statistics[k]['goal'] == 'unit':
                unit_name = GENERAL_ACTION_INFO_MASK[k]['name'].split('_')[1]
                units_num[unit_name] = self.cumulative_statistics[k]['count']
                max_num = max(self.cumulative_statistics[k]['count'], max_num)
        for k in units_num.keys():
            units_num[k] /= (1.0 * max_num)
        return units_num


class GameLoopStatistics:
    """
    Overview: Human replay data statistics specified by game loop
    """

    def __init__(self, stat, begin_num=20):
        self.ori_stat = stat
        self.ori_stat = self.add_game_loop(self.ori_stat)
        self.begin_num = begin_num
        self.mmr = 6200
        self._clip_global_bo()
        self.cache_reward_z = None
        self.cache_input_z = None
        self.max_game_loop = self.ori_stat['cumulative_stat'][-1]['game_loop']
        self._init_global_z()

    def add_game_loop(self, stat):
        beginning_build_order = stat['beginning_build_order']
        cumulative_stat = stat['cumulative_stat']
        if 'game_loop' in beginning_build_order[0].keys():
            return stat

        def is_action_frame(action_type, cum_idx):
            # for start case
            if cum_idx == 0:
                return action_type in cumulative_stat[cum_idx].keys()
            last_frame = cumulative_stat[cum_idx - 1]
            cur_frame = cumulative_stat[cum_idx]
            miss_key = cur_frame.keys() - last_frame.keys()
            diff_count_key = set()
            for k in last_frame.keys():
                if k != 'game_loop' and cur_frame[k]['count'] != last_frame[k]['count']:
                    diff_count_key.add(k)
            diff_key = miss_key.union(diff_count_key)
            return action_type in diff_key

        cum_idx = 0
        new_beginning_build_order = []
        for i in range(len(beginning_build_order)):
            item = beginning_build_order[i]
            action_type = item['action_type']
            while cum_idx < len(cumulative_stat) and not is_action_frame(action_type, cum_idx):
                cum_idx += 1
            if cum_idx < len(cumulative_stat):
                item.update({'game_loop': cumulative_stat[cum_idx]['game_loop']})
                new_beginning_build_order.append(item)
            cum_idx += 1

        new_stat = stat
        new_stat['beginning_build_order'] = new_beginning_build_order
        new_stat['begin_game_loop'] = [t['game_loop'] for t in new_beginning_build_order]
        new_stat['cum_game_loop'] = [t['game_loop'] for t in new_stat['cumulative_stat']]
        return new_stat

    def _clip_global_bo(self):
        beginning_build_order = copy.deepcopy(self.ori_stat['beginning_build_order'])
        if len(beginning_build_order) < self.begin_num:
            # the input_global_bo will be padded up to begin_num when transformed into input format
            self.input_global_bo = beginning_build_order
            self.reward_global_bo = beginning_build_order
        else:
            beginning_build_order = beginning_build_order[:self.begin_num]
            self.input_global_bo = beginning_build_order
            self.reward_global_bo = beginning_build_order

    def _init_global_z(self):
        # init input_global_z
        beginning_build_order, cumulative_stat = self.input_global_bo, self.ori_stat['cumulative_stat'][-1]
        self.input_global_z = transformed_stat_mmr(
            {
                'begin_statistics': beginning_build_order,
                'cumulative_statistics': cumulative_stat
            }, self.mmr, self.begin_num
        )
        # init reward_global_z
        beginning_build_order, cumulative_stat = self.reward_global_bo, self.ori_stat['cumulative_stat'][-1]
        cum_stat_tensor = transform_cum_stat(cumulative_stat)
        self.reward_global_z = {
            'built_unit': cum_stat_tensor['unit_build'],
            'effect': cum_stat_tensor['effect'],
            'upgrade': cum_stat_tensor['research'],
            'build_order': transform_build_order_to_z_format(beginning_build_order),
        }
        self.reward_global_z = to_dtype(self.reward_global_z, torch.long)

    def get_input_z_by_game_loop(self, game_loop, cumulative_stat=None):
        """
        Note: if game_loop is None, load global stat
        """
        if cumulative_stat is None:
            if game_loop is None:
                return self.input_global_z
            else:
                _, cumulative_stat = self._get_stat_by_game_loop(game_loop)
        beginning_build_order = self.input_global_bo
        ret = transformed_stat_mmr(
            {
                'begin_statistics': beginning_build_order,
                'cumulative_statistics': cumulative_stat
            }, self.mmr, self.begin_num
        )
        return ret

    def get_reward_z_by_game_loop(self, game_loop, build_order_length=None):
        """
        Note: if game_loop is None, load global stat
        """
        if game_loop is None:
            global_z = copy.deepcopy(self.reward_global_z)
            global_z['build_order']['type'] = global_z['build_order']['type'][:build_order_length]
            global_z['build_order']['loc'] = global_z['build_order']['loc'][:build_order_length]
            return global_z
        else:
            beginning_build_order, cumulative_stat = self._get_stat_by_game_loop(game_loop)

        cum_stat_tensor = transform_cum_stat(cumulative_stat)
        ret = {
            'built_unit': cum_stat_tensor['unit_build'],
            'effect': cum_stat_tensor['effect'],
            'upgrade': cum_stat_tensor['research'],
            'build_order': transform_build_order_to_z_format(beginning_build_order),
        }
        ret = to_dtype(ret, torch.long)
        return ret

    def _get_stat_by_game_loop(self, game_loop):
        begin_idx = binary_search(self.ori_stat['begin_game_loop'], game_loop)
        cum_idx = binary_search(self.ori_stat['cum_game_loop'], game_loop)
        return self.ori_stat['beginning_build_order'][:begin_idx + 1], self.ori_stat['cumulative_stat'][cum_idx]

    def excess_max_game_loop(self, agent_game_loop):
        return agent_game_loop > self.max_game_loop


def transform_build_order_to_z_format(stat):
    """
    Overview: transform beginning_build_order to the format to calculate reward
    stat: list->element: dict('action_type': int, 'location': list(len=2)->element: int)
    """
    ret = {'type': np.zeros(len(stat), dtype=np.int), 'loc': np.empty((len(stat), 2), dtype=np.int)}
    zeroxy = np.array([0, 0], dtype=np.int)
    for n in range(len(stat)):
        action_type, location = stat[n]['action_type'], stat[n]['location']
        ret['type'][n] = action_type
        ret['loc'][n] = location if isinstance(location, list) else zeroxy
    ret['type'] = torch.Tensor(ret['type'])
    ret['loc'] = torch.Tensor(ret['loc'])
    return ret


def transform_build_order_to_input_format(stat, begin_num, location_num=LOCATION_BIT_NUM):
    """
    Overview: transform beginning_build_order to the format for input
    stat: list->element: dict('action_type': int, 'location': list(len=2)->element: int)
    """
    beginning_build_order_tensor = []
    for item in stat:
        action_type, location = item['action_type'], item['location']
        if action_type == 0:
            action_type = torch.zeros(NUM_BEGIN_ACTIONS)
        else:
            action_type = torch.LongTensor([action_type])
            action_type = reorder_one_hot_array(action_type, BEGIN_ACTIONS_REORDER_ARRAY, num=NUM_BEGIN_ACTIONS)
        if isinstance(location, list):
            x = batch_binary_encode(torch.LongTensor([location[0]]), bit_num=location_num)[0]
            y = batch_binary_encode(torch.LongTensor([location[1]]), bit_num=location_num)[0]
            location = torch.cat([x, y], dim=0)
        else:
            location = torch.zeros(location_num * 2)
        beginning_build_order_tensor.append(torch.cat([action_type.squeeze(0), location], dim=0))
    if len(stat):
        beginning_build_order_tensor = torch.stack(beginning_build_order_tensor, dim=0)
    else:
        return torch.zeros(begin_num, 194)
    # pad
    if beginning_build_order_tensor.shape[0] < begin_num:
        miss_num = begin_num - beginning_build_order_tensor.shape[0]
        pad_part = torch.zeros(miss_num, beginning_build_order_tensor.shape[1])
        beginning_build_order_tensor = torch.cat([beginning_build_order_tensor, pad_part], dim=0)
    return beginning_build_order_tensor[:begin_num]


def transform_cum_stat(cumulative_stat):
    """
    Overview: transform cumulative_stat to the format for both input and reward
    cumulative_stat: dict('action_type': {'goal': str, count: int})
    """
    cumulative_stat_tensor = {
        'unit_build': torch.zeros(NUM_UNIT_BUILD_ACTIONS),
        'effect': torch.zeros(NUM_EFFECT_ACTIONS),
        'research': torch.zeros(NUM_RESEARCH_ACTIONS)
    }
    for k, v in cumulative_stat.items():
        if k == 'game_loop':
            continue
        if v['goal'] in ['unit', 'build']:
            cumulative_stat_tensor['unit_build'][UNIT_BUILD_ACTIONS_REORDER_ARRAY[k]] = 1
        elif v['goal'] in ['effect']:
            cumulative_stat_tensor['effect'][EFFECT_ACTIONS_REORDER_ARRAY[k]] = 1
        elif v['goal'] in ['research']:
            cumulative_stat_tensor['research'][RESEARCH_ACTIONS_REORDER_ARRAY[k]] = 1
    return cumulative_stat_tensor


def transform_stat(stat, meta, begin_num):
    mmr = meta['home_mmr']
    return transformed_stat_mmr(stat, mmr, begin_num)


def transformed_stat_mmr(stat, mmr, begin_num):
    """
    Overview: transform replay metadata and statdata to input stat(mmr + z)
    """
    beginning_build_order = stat['begin_statistics']
    beginning_build_order_tensor = transform_build_order_to_input_format(beginning_build_order, begin_num)
    cumulative_stat_tensor = transform_cum_stat(stat['cumulative_statistics'])
    mmr = torch.LongTensor([mmr])
    mmr = div_one_hot(mmr, 6000, 1000).squeeze(0)
    return {
        'mmr': mmr,
        'beginning_build_order': beginning_build_order_tensor,
        'cumulative_stat': cumulative_stat_tensor
    }


def transform_stat_processed(old_stat_processed):
    """
    Overview: transform new begin action(for stat_processed)
    """
    new_stat_processed = copy.deepcopy(old_stat_processed)
    beginning_build_order = new_stat_processed['beginning_build_order']
    new_beginning_build_order = []
    location_dim = 2 * LOCATION_BIT_NUM
    for item in beginning_build_order:
        action_type, location = item[:-location_dim], item[-location_dim:]
        action_type = torch.nonzero(action_type).item()
        action_type = OLD_BEGIN_ACTIONS_REORDER_INV[action_type]
        if action_type not in BEGIN_ACTIONS:
            continue
        action_type = BEGIN_ACTIONS_REORDER_ARRAY[action_type]
        action_type = torch.LongTensor([action_type])
        action_type = one_hot(action_type, NUM_BEGIN_ACTIONS)[0]
        new_item = torch.cat([action_type, location], dim=0)
        new_beginning_build_order.append(new_item)
    new_stat_processed['beginning_build_order'] = torch.stack(new_beginning_build_order, dim=0)
    return new_stat_processed


def transform_stat_professional_player(old_stat):
    new_stat = copy.deepcopy(old_stat)
    beginning_build_order = new_stat['beginning_build_order']
    new_beginning_build_order = []
    for item in beginning_build_order:
        if item['action_type'] in BEGIN_ACTIONS:
            new_beginning_build_order.append(item)
    new_stat['beginning_build_order'] = new_beginning_build_order
    return new_stat


class StatKey:

    def __init__(self, home_race=None, away_race=None, map_name=None, player_id=None):
        self.home_race = home_race
        self.away_race = away_race
        self.map_name = map_name
        self.player_id = player_id

    @classmethod
    def check_path(cls, item):
        """
        Overview: check stat path name format
        Note:
            format template: homerace_awayrace_mapname_playerid_id
        """
        race_list = ['zerg', 'terran', 'protoss']
        map_list = ['KingsCove', 'KairosJunction', 'NewRepugnancy', 'CyberForest']
        try:
            item_contents = item.split('_')
            assert len(item_contents) == 5
            assert item_contents[0] in race_list
            assert item_contents[1] in race_list
            assert item_contents[2] in map_list
            assert item_contents[3] in ['1', '2']
        except Exception as e:
            print(item_contents)
            return False
        return True

    @classmethod
    def path2key(cls, path):
        items = path.split('_')[:4]
        return StatKey(*items)

    def match(self, other):
        assert isinstance(other, StatKey)
        for k, v in self.__dict__.items():
            if v is not None:
                if other.__dict__[k] != v:
                    return False
        return True

    def __repr__(self):
        return 'h_race: {}\ta_race: {}\tmap: {}\tid: {}'.format(
            self.home_race, self.away_race, self.map_name, self.player_id
        )


class StatManager:

    def __init__(self, dirname, stat_path_list):
        with open(stat_path_list, 'r') as f:
            data = f.readlines()
            data = [t.strip() for t in data]
        self.stat_paths = [item for item in data if StatKey.check_path(item)]
        self.stat_keys = [StatKey.path2key(t) for t in self.stat_paths]
        self.dirname = dirname

    def get_ava_stats(self, **kwargs):
        assert kwargs['player_id'] == 'ava'
        # select matched results
        stats = []
        for player_id in ['1', '2']:
            kwargs['player_id'] = player_id
            query = StatKey(**kwargs)
            matched_results_idx = [idx for idx, t in enumerate(self.stat_keys) if query.match(t)]
            if len(matched_results_idx) == 0:
                raise RuntimeError("no matched stat, input kwargs are: {}".format(kwargs))
            # random sample
            selected_idx = np.random.choice(matched_results_idx)
            stat_path = self.stat_paths[selected_idx]
            stats.append(stat_path)
        stats = [os.path.join(self.dirname, s) for s in stats]
        return stats
