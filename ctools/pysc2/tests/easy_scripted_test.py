#!/usr/bin/python
# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Solve the nm_easy map using a fixed policy by reading the feature layers."""

from absl.testing import absltest

from ctools.pysc2.agents import scripted_agent
from ctools.pysc2.env import run_loop
from ctools.pysc2.env import sc2_env
from ctools.pysc2.tests import utils


class TestEasy(utils.TestCase):
  steps = 200
  step_mul = 16

  def test_move_to_beacon(self):
    with sc2_env.SC2Env(
        map_name="MoveToBeacon",
        players=[sc2_env.Agent(sc2_env.Race.terran)],
        agent_interface_format=sc2_env.AgentInterfaceFormat(
            feature_dimensions=sc2_env.Dimensions(
                screen=84,
                minimap=64)),
        step_mul=self.step_mul,
        game_steps_per_episode=self.steps * self.step_mul) as env:
      agent = scripted_agent.MoveToBeacon()
      run_loop.run_loop([agent], env, self.steps)

    # Get some points
    self.assertLessEqual(agent.episodes, agent.reward)
    self.assertEqual(agent.steps, self.steps)

  def test_collect_mineral_shards(self):
    with sc2_env.SC2Env(
        map_name="CollectMineralShards",
        players=[sc2_env.Agent(sc2_env.Race.terran)],
        agent_interface_format=sc2_env.AgentInterfaceFormat(
            feature_dimensions=sc2_env.Dimensions(
                screen=84,
                minimap=64)),
        step_mul=self.step_mul,
        game_steps_per_episode=self.steps * self.step_mul) as env:
      agent = scripted_agent.CollectMineralShards()
      run_loop.run_loop([agent], env, self.steps)

    # Get some points
    self.assertLessEqual(agent.episodes, agent.reward)
    self.assertEqual(agent.steps, self.steps)

  def test_collect_mineral_shards_feature_units(self):
    with sc2_env.SC2Env(
        map_name="CollectMineralShards",
        players=[sc2_env.Agent(sc2_env.Race.terran)],
        agent_interface_format=sc2_env.AgentInterfaceFormat(
            feature_dimensions=sc2_env.Dimensions(
                screen=84,
                minimap=64),
            use_feature_units=True),
        step_mul=self.step_mul,
        game_steps_per_episode=self.steps * self.step_mul) as env:
      agent = scripted_agent.CollectMineralShardsFeatureUnits()
      run_loop.run_loop([agent], env, self.steps)

    # Get some points
    self.assertLessEqual(agent.episodes, agent.reward)
    self.assertEqual(agent.steps, self.steps)

  def test_collect_mineral_shards_raw(self):
    with sc2_env.SC2Env(
        map_name="CollectMineralShards",
        players=[sc2_env.Agent(sc2_env.Race.terran)],
        agent_interface_format=sc2_env.AgentInterfaceFormat(
            action_space=sc2_env.ActionSpace.RAW,  # or: use_raw_actions=True,
            use_raw_units=True),
        step_mul=self.step_mul,
        game_steps_per_episode=self.steps * self.step_mul) as env:
      agent = scripted_agent.CollectMineralShardsRaw()
      run_loop.run_loop([agent], env, self.steps)

    # Get some points
    self.assertLessEqual(agent.episodes, agent.reward)
    self.assertEqual(agent.steps, self.steps)

  def test_defeat_roaches(self):
    with sc2_env.SC2Env(
        map_name="DefeatRoaches",
        players=[sc2_env.Agent(sc2_env.Race.terran)],
        agent_interface_format=sc2_env.AgentInterfaceFormat(
            feature_dimensions=sc2_env.Dimensions(
                screen=84,
                minimap=64)),
        step_mul=self.step_mul,
        game_steps_per_episode=self.steps * self.step_mul) as env:
      agent = scripted_agent.DefeatRoaches()
      run_loop.run_loop([agent], env, self.steps)

    # Get some points
    self.assertLessEqual(agent.episodes, agent.reward)
    self.assertEqual(agent.steps, self.steps)

  def test_defeat_roaches_raw(self):
    with sc2_env.SC2Env(
        map_name="DefeatRoaches",
        players=[sc2_env.Agent(sc2_env.Race.terran)],
        agent_interface_format=sc2_env.AgentInterfaceFormat(
            action_space=sc2_env.ActionSpace.RAW,  # or: use_raw_actions=True,
            use_raw_units=True),
        step_mul=self.step_mul,
        game_steps_per_episode=100*self.steps * self.step_mul) as env:
      agent = scripted_agent.DefeatRoachesRaw()
      run_loop.run_loop([agent], env, self.steps)

    # Get some points
    self.assertLessEqual(agent.episodes, agent.reward)
    self.assertEqual(agent.steps, self.steps)


if __name__ == "__main__":
  absltest.main()
