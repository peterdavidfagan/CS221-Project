# Implementation of DDPG algorithm with inspiration from 
# "https://github.com/ghliu/pytorch-ddpg/blob/master/ddpg.py"
import robosuite as suite
import numpy as np
import math

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.distributions as dist
from torch.autograd import Variable
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

from roboray.models.utils import *


class DDPGActor(nn.Module):
    '''
    Pytorch neural network for Actor model

    :param 
    '''

    def __init__(self, state_dim, action_dim, hidden_size, init_w=3e-3):
        super(DDPGActor, self).__init__()
        self.l1 = nn.Linear(state_dim, hidden_size)
        self.bn1 = nn.LayerNorm(hidden_size)
        self.l2 = nn.Linear(hidden_size, hidden_size)
        self.bn2 = nn.LayerNorm(hidden_size)
        self.l3 = nn.Linear(hidden_size, action_dim)
        self.init_weights(init_w)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.to(self.device)

    def init_weights(self, init_w):
        self.l1.weight.data = fanin_init(self.l1.weight.data.size())
        self.l2.weight.data = fanin_init(self.l2.weight.data.size())
        self.l3.weight.data.uniform_(-init_w, init_w)

    def forward(self, x):
        x = F.relu(self.bn1(self.l1(x)))
        x = F.relu(self.bn2(self.l2(x)))
        x = torch.tanh(self.l3(x))

        return x


class DDPGCritic(nn.Module):
    '''
    Pytorch neural network for critic model

    :param 
    '''

    def __init__(self, state_dim, action_dim, hidden_size, init_w=3e-3):
        super(DDPGCritic, self).__init__()
        self.l1 = nn.Linear(state_dim, hidden_size)
        self.bn1 = nn.LayerNorm(hidden_size)
        self.l2 = nn.Linear(hidden_size+action_dim, hidden_size)
        self.bn2 = nn.LayerNorm(hidden_size)
        self.l3 = nn.Linear(hidden_size, 1)
        self.init_weights(init_w)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.to(self.device)

    def init_weights(self, init_w):
        self.l1.weight.data = fanin_init(self.l1.weight.data.size())
        self.l2.weight.data = fanin_init(self.l2.weight.data.size())
        self.l3.weight.data.uniform_(-init_w, init_w)

    def forward(self, xs):
        x, a = xs
        x = F.relu(self.bn1(self.l1(x)))
        x = F.relu(self.bn2(self.l2(torch.cat([x,a],1))))
        x = self.l3(x)

        return x


class DDPG:
    '''
    Implementation of Deep Deterministic Policy Gradient according to 
    https://arxiv.org/pdf/1509.02971.pdf

    :param
    '''

    def __init__(self, state_dim, action_dim, env, args):

        self.state_dim = state_dim
        self.action_dim = action_dim

        self.actor = DDPGActor(state_dim, action_dim, args.hidden_size)
        self.actor_target = DDPGActor(state_dim, action_dim, args.hidden_size)
        self.actor_optim = optim.Adam(self.actor.parameters(), lr=args.lr_actor)

        self.critic = DDPGCritic(state_dim, action_dim, args.hidden_size)
        self.critic_target = DDPGCritic(state_dim, action_dim, args.hidden_size)
        self.critic_optim = optim.Adam(self.critic.parameters(), lr=args.lr_critic)
        self.criterion = nn.MSELoss()
        self.env = env

        hard_update(self.actor_target, self.actor)
        hard_update(self.critic_target, self.critic)

        self.max_mem_size = args.max_mem_size
        self.memory = ReplayBufferWithHindsight(ReplayBuffer(args.max_mem_size), args.enable_her, Lift_reward_func1, self.env)

        self.random_process = OUActionNoise(mu=np.zeros(action_dim))

        self.tau = args.tau
        self.batch_size = args.batch_size
        self.lr = args.lr
        self.lr_actor = args.lr_actor
        self.lr_critic = args.lr_critic
        self.gamma = args.gamma
        self.epsilon = 1.0
        self.depsilon = 1.0 / args.epsilon

        self.s_t = None
        self.a_t = None
        self.is_training = True


    def observe(self, r_t, s_t1, done):
        if self.is_training:
            self.memory.store_transition(self.s_t, self.a_t, r_t, s_t1, done)
            self.s_t = s_t1


    def select_action(self, state, decay_epsilon=True):
        self.actor.eval()
        action = self.actor(to_tensor(state).to(self.actor.device)).to('cpu').detach().numpy()
        action += self.is_training*max(self.epsilon, 0)*self.random_process()
        action = np.clip(action, -1., 1.)

        if decay_epsilon:
            self.epsilon -= self.depsilon

        self.actor.train()
        self.a_t = action
        return action

    def random_action(self):
        action = np.random.uniform(-1.,1.,self.action_dim)
        self.a_t = action
        return action

    def update_parameters(self):
        # Sample batch from replay buffer
        state_batch, action_batch, reward_batch, \
        next_state_batch, done_batch = self.memory.sample(self.batch_size)
        state_batch = to_tensor(state_batch).to(device)
        action_batch = to_tensor(action_batch).to(device)
        reward_batch = to_tensor(reward_batch).to(device)
        next_state_batch = to_tensor(next_state_batch).to(device)
        done_batch = to_tensor(done_batch).to(device)

        # Calculate next q-values
        with torch.no_grad():
            q_next = self.critic_target([next_state_batch, \
                         self.actor_target(next_state_batch)])

            target_q_batch = reward_batch + \
                self.gamma*q_next   # Need to add details for terminal case

        # Critic update
        self.critic.zero_grad()
        self.critic.train()

        q_batch = self.critic([state_batch, action_batch])
        critic_loss = self.criterion(q_batch, target_q_batch)
        critic_loss.backward()
        self.critic_optim.step()

        # Actor update 
        self.critic.eval()
        self.actor.zero_grad()
        self.actor.train()

        actor_loss = self.critic([
            state_batch,
            self.actor(state_batch)
        ])

        actor_loss = -actor_loss.mean()
        actor_loss.backward()
        self.actor_optim.step()

        # Target update
        soft_update(self.actor_target, self.actor, self.tau)
        soft_update(self.critic_target, self.critic, self.tau)
        
  