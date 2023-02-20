import copy
from math import gamma

from torch.serialization import validate_cuda_device
from components.episode_buffer import EpisodeBatch
from modules.mixers.vdn import VDNMixer
from modules.mixers.qmix import QMixer
from modules.mixers.flex_qmix import FlexQMixer, LinearFlexQMixer
import torch as th
from torch.optim import RMSprop, optimizer
from torch.distributions import kl_divergence
import torch.distributions as D


class MsgQLearner:
    def __init__(self, mac, scheme, logger, args):
        self.args = args
        self.mac = mac
        self.logger = logger
        if args.mac == "rlcomm_mac":
            self.params = list(mac.parameters())
            self.elector_params = list(mac.elector_parameters())
        else:
            self.params = list(mac.parameters())

        self.last_target_update_episode = 0
        self.mixer = None
        if args.mixer is not None:
            if args.mixer == "vdn":
                self.mixer = VDNMixer()
            elif args.mixer == "qmix":
                self.mixer = QMixer(args)
            elif args.mixer == "flex_qmix":
                assert args.entity_scheme, "FlexQMixer only available with entity scheme"
                self.mixer = FlexQMixer(args)
            elif args.mixer == "lin_flex_qmix":
                assert args.entity_scheme, "FlexQMixer only available with entity scheme"
                self.mixer = LinearFlexQMixer(args)
            else:
                raise ValueError("Mixer {} not recognised.".format(args.mixer))
            self.params += list(self.mixer.parameters())
            self.target_mixer = copy.deepcopy(self.mixer)

        self.optimiser = RMSprop(params=self.params, lr=args.lr, alpha=args.optim_alpha, eps=args.optim_eps,
                                 weight_decay=args.weight_decay)
        if args.mac == "rlcomm_mac":
            self.elector_optim = RMSprop(params=self.elector_params, lr=args.lr,
                alpha=args.optim_alpha, eps=args.optim_eps,
                weight_decay=args.weight_decay)
        # a little wasteful to deepcopy (e.g. duplicates action selector), but should work for any MAC
        self.target_mac = copy.deepcopy(mac)

        self.log_stats_t = -self.args.learner_log_interval - 1
        if args.mac == "rlcomm_mac":
            self.log_stats_t_elector = -self.args.learner_log_interval - 1
            # share the same head selector between target_mac and mac
            self.target_mac.elector = self.mac.elector

    def _get_mixer_ins(self, batch, repeat_batch=1):
        if not self.args.entity_scheme:
            return (batch["state"][:, :-1].repeat(repeat_batch, 1, 1),
                    batch["state"][:, 1:])
        else:
            entities = []
            bs, max_t, ne, ed = batch["entities"].shape
            entities.append(batch["entities"])
            if self.args.entity_last_action:
                last_actions = th.zeros(bs, max_t, ne, self.args.n_actions,
                                        device=batch.device,
                                        dtype=batch["entities"].dtype)
                last_actions[:, 1:, :self.args.n_agents] = batch["actions_onehot"][:, :-1]
                entities.append(last_actions)

            entities = th.cat(entities, dim=3)
            return ((entities[:, :-1].repeat(repeat_batch, 1, 1, 1),
                     batch["entity_mask"][:, :-1].repeat(repeat_batch, 1, 1)),
                    (entities[:, 1:],
                     batch["entity_mask"][:, 1:]))

    def train(self, batch: EpisodeBatch, t_env: int, episode_num: int):
        # Get the relevant quantities
        rewards = batch["reward"][:, :-1]
        actions = batch["actions"][:, :-1]
        terminated = batch["terminated"][:, :-1].float()
        mask = batch["filled"][:, :-1].float()
        mask[:, 1:] = mask[:, 1:] * (1 - terminated[:, :-1])
        avail_actions = batch["avail_actions"]
        p_msg = batch["self_message"]
        h_msg = batch["head_message"]

        will_log = (t_env - self.log_stats_t >= self.args.learner_log_interval)

        # # Calculate estimated Q-Values
        # mac_out = []
        self.mac.init_hidden(batch.batch_size)
        # enable things like dropout on mac and mixer, but not target_mac and target_mixer
        self.mac.train()
        self.mixer.train()
        self.target_mac.eval()
        self.target_mixer.eval()

        if 'imagine' in self.args.agent:
            
            all_mac_out, groups, _, _, msg_dis_mv, msg_dis_inf_mv = self.mac.forward(batch, t=None, imagine=True, train_mode=True, 
                                                   use_gt_factors=self.args.train_gt_factors,
                                                   use_rand_gt_factors=self.args.train_rand_gt_factors)
            # Pick the Q-Values for the actions taken by each agent
            rep_actions = actions.repeat(3, 1, 1, 1)
            all_chosen_action_qvals = th.gather(all_mac_out[:, :-1], dim=3, index=rep_actions).squeeze(3)  # Remove the last dim

            mac_out, moW, moI = all_mac_out.chunk(3, dim=0)
            chosen_action_qvals, caqW, caqI = all_chosen_action_qvals.chunk(3, dim=0)
            caq_imagine = th.cat([caqW, caqI], dim=2)

            if will_log and self.args.test_gt_factors:
                gt_all_mac_out, gt_groups = self.mac.forward(batch, t=None, imagine=True, use_gt_factors=True)
                # Pick the Q-Values for the actions taken by each agent
                gt_all_chosen_action_qvals = th.gather(gt_all_mac_out[:, :-1], dim=3, index=rep_actions).squeeze(3)  # Remove the last dim

                gt_mac_out, gt_moW, gt_moI = gt_all_mac_out.chunk(3, dim=0)
                gt_chosen_action_qvals, gt_caqW, gt_caqI = gt_all_chosen_action_qvals.chunk(3, dim=0)
                gt_caq_imagine = th.cat([gt_caqW, gt_caqI], dim=2)
        else:
            mac_out, _, _, msg_dis_mv, msg_dis_inf_mv = self.mac.forward(batch, t=None, train_mode=True)
            # Pick the Q-Values for the actions taken by each agent
            chosen_action_qvals = th.gather(mac_out[:, :-1], dim=3, index=actions).squeeze(3)  # Remove the last dim

        self.target_mac.init_hidden(batch.batch_size)

        target_mac_out, _, _, _, _ = self.target_mac.forward(batch, t=None, train_mode=True)
        avail_actions_targ = avail_actions
        target_mac_out = target_mac_out[:, 1:]

        # Mask out unavailable actions
        target_mac_out[avail_actions_targ[:, 1:] == 0] = -9999999  # From OG deepmarl

        # Max over target Q-Values
        if self.args.double_q:
            # Get actions that maximise live Q (for double q-learning)
            mac_out_detach = mac_out.clone().detach()
            mac_out_detach[avail_actions_targ == 0] = -9999999
            cur_max_actions = mac_out_detach[:, 1:].max(dim=3, keepdim=True)[1]
            target_max_qvals = th.gather(target_mac_out, 3, cur_max_actions).squeeze(3)
        else:
            target_max_qvals = target_mac_out.max(dim=3)[0]

        # Mix
        if self.mixer is not None:
            if 'imagine' in self.args.agent:
                mix_ins, targ_mix_ins = self._get_mixer_ins(batch)
                chosen_action_qvals = self.mixer(chosen_action_qvals,
                                                 mix_ins)
                # don't need last timestep
                groups = [gr[:, :-1] for gr in groups]
                if will_log and self.args.test_gt_factors:
                    caq_imagine, ingroup_prop = self.mixer(
                        caq_imagine, mix_ins,
                        imagine_groups=groups,
                        ret_ingroup_prop=True)
                    gt_groups = [gr[:, :-1] for gr in gt_groups]
                    gt_caq_imagine, gt_ingroup_prop = self.mixer(
                        gt_caq_imagine, mix_ins,
                        imagine_groups=gt_groups,
                        ret_ingroup_prop=True)
                else:
                    caq_imagine = self.mixer(caq_imagine, mix_ins,
                                             imagine_groups=groups)
            else:
                mix_ins, targ_mix_ins = self._get_mixer_ins(batch)
                chosen_action_qvals = self.mixer(chosen_action_qvals, mix_ins)
            target_max_qvals = self.target_mixer(target_max_qvals, targ_mix_ins)

        # Calculate 1-step Q-Learning targets
        targets = rewards + self.args.gamma * (1 - terminated) * target_max_qvals

        # Td-error
        td_error = (chosen_action_qvals - targets.detach())
        mask = mask.expand_as(td_error)
        # 0-out the targets that came from padded data
        masked_td_error = td_error * mask
        # Normal L2 loss, take mean over actual data
        q_loss = (masked_td_error ** 2).sum() / mask.sum()

        if 'imagine' in self.args.agent:
            im_prop = self.args.lmbda
            im_td_error = (caq_imagine - targets.detach())
            im_masked_td_error = im_td_error * mask
            im_loss = (im_masked_td_error ** 2).sum() / mask.sum()
            q_loss = (1 - im_prop) * q_loss + im_prop * im_loss
        #maxmize the MI between message encoder and future T steps trajectories
        loss= q_loss
        if not self.args.no_summary:
            msg_dis_inf = D.Normal(msg_dis_inf_mv.mean[:,self.args.msg_T:,:,:], msg_dis_inf_mv.scale[:,self.args.msg_T:,:,:])
            msg_dis = D.Normal(msg_dis_mv.mean[:,:-self.args.msg_T,:,:], msg_dis_mv.scale[:,:-self.args.msg_T,:,:])
            entropy_loss = -msg_dis.entropy().sum(dim=-1).mean() *self.args.msg_entropy_weight
            kl_loss = kl_divergence(msg_dis, msg_dis_inf).sum(dim=-1).mean() * self.args.msg_ce_weight
            loss += entropy_loss + kl_loss
            if self.args.ceb_weight > 0:
                bs, t, ne, msg_d = msg_dis_mv.mean[:,:-self.args.msg_T,:,:].shape
                #Don't do softmax on time dimension since the limitation of CUDA memory.
                da = D.Normal(msg_dis_mv.mean[:,:-self.args.msg_T,:,:].permute(1,0,2,3).reshape(t, 1, bs*ne, msg_d),\
                            msg_dis_mv.scale[:,:-self.args.msg_T,:,:].permute(1,0,2,3).reshape(t, 1, bs*ne, msg_d))
                db = D.Normal(msg_dis_inf_mv.mean[:,self.args.msg_T:,:,:].permute(1,0,2,3).reshape(t, 1, bs*ne, msg_d), \
                            msg_dis_inf_mv.scale[:,self.args.msg_T:,:,:].permute(1,0,2,3).reshape(t, 1, bs*ne, msg_d))

                z=da.sample() #bs*t*ne*msg_d
                if self.args.ceb_kl_weight > 0:
                    ez = da.log_prob(z) #t*1*(bs*ne)*msg_d
                    bz = db.log_prob(z) #t*1*(bs*ne)*msg_d
                    ceb_kl_loss = self.args.ceb_kl_weight * (ez-bz).sum(-1).mean()
                    loss += ceb_kl_loss
                z=z.reshape(t, bs*ne, 1, msg_d)
                logits = db.log_prob(z) #t*(bs*ne)*(bs*ne)*msg_d
                logits = logits.sum(-1) #t*(bs*ne)*(bs*ne)
                ince = D.Categorical(logits=logits) #t*(bs*ne)
                inds = th.arange(bs*ne, device=batch.device).unsqueeze(0).repeat(t,1) #t*(bs*ne)
                ceb_loss = -self.args.ceb_weight * ince.log_prob(inds).mean()
                loss += ceb_loss
        # Optimise
        self.optimiser.zero_grad()
        loss.backward()
        grad_norm = th.nn.utils.clip_grad_norm_(self.params, self.args.grad_norm_clip)
        try:
            grad_norm=grad_norm.item()
        except:
            pass
        self.optimiser.step()

        if (episode_num - self.last_target_update_episode) / self.args.target_update_interval >= 1.0:
            self._update_targets()
            self.last_target_update_episode = episode_num

        if t_env - self.log_stats_t >= self.args.learner_log_interval:
            self.logger.log_stat("loss", loss.item(), t_env)
            self.logger.log_stat("q_loss", q_loss.item(), t_env)
            if not self.args.no_summary:
                self.logger.log_stat("kl_loss", kl_loss.item(), t_env)
                self.logger.log_stat("msg_dis_mean", msg_dis.mean.mean().item(), t_env)
                self.logger.log_stat("msg_dis_inf_mean", msg_dis_inf.mean.mean().item(), t_env)
                self.logger.log_stat("msg_dis_var", msg_dis.scale.mean().item(), t_env)
                self.logger.log_stat("msg_dis_inf_var", msg_dis_inf.scale.mean().item(), t_env)
                self.logger.log_stat("entropy_loss", entropy_loss.item(), t_env)
                if self.args.ceb_weight > 0:
                    self.logger.log_stat("ceb_loss", ceb_loss.item(), t_env)
                    if self.args.ceb_kl_weight > 0:
                        self.logger.log_stat("ceb_kl_loss", ceb_kl_loss.item(), t_env)
            
            if 'imagine' in self.args.agent:
                self.logger.log_stat("im_loss", im_loss.item(), t_env)
            
            if self.args.test_gt_factors:
                self.logger.log_stat("ingroup_prop", ingroup_prop.item(), t_env)
                self.logger.log_stat("gt_ingroup_prop", gt_ingroup_prop.item(), t_env)
            self.logger.log_stat("grad_norm", grad_norm, t_env)
            mask_elems = mask.sum().item()
            self.logger.log_stat("td_error_abs", (masked_td_error.abs().sum().item()/mask_elems), t_env)
            self.logger.log_stat("q_taken_mean", (chosen_action_qvals * mask).sum().item()/(mask_elems * self.args.n_agents), t_env)
            self.logger.log_stat("target_mean", (targets * mask).sum().item()/(mask_elems * self.args.n_agents), t_env)
            if batch.max_seq_length == 2:
                # We are in a 1-step env. Calculate the max Q-Value for logging
                max_agent_qvals = mac_out_detach[:,0].max(dim=2, keepdim=True)[0]
                max_qtots = self.mixer(max_agent_qvals, batch["state"][:,0])
                self.logger.log_stat("max_qtot", max_qtots.mean().item(), t_env)
            self.log_stats_t = t_env

    def _update_targets(self):
        self.target_mac.load_state(self.mac)
        if self.mixer is not None:
            self.target_mixer.load_state_dict(self.mixer.state_dict())
        self.logger.console_logger.info("Updated target network")

    def cuda(self):
        self.mac.cuda()
        self.target_mac.cuda()
        if self.mixer is not None:
            self.mixer.cuda()
            self.target_mixer.cuda()

    def save_models(self, path):
        self.mac.save_models(path)
        if self.mixer is not None:
            th.save(self.mixer.state_dict(), "{}/mixer.th".format(path))
        th.save(self.optimiser.state_dict(), "{}/opt.th".format(path))

    def load_models(self, path, evaluate=False):
        self.mac.load_models(path)
        # Not quite right but I don't want to save target networks
        self.target_mac.load_models(path)
        if not evaluate:
            if self.mixer is not None:
                self.mixer.load_state_dict(th.load("{}/mixer.th".format(path), map_location=lambda storage, loc: storage))
            self.optimiser.load_state_dict(th.load("{}/opt.th".format(path), map_location=lambda storage, loc: storage))
    def train_elector(self, batch: EpisodeBatch, t_env:int, episode_num:int):
        if self.args.mac == "rlcomm_mac":
            # Get the relevant quantities
            rewards = batch["reward"][:, :-1] # B, t_max, 1
            pi = batch["head_probs"][:,:-1]
            # TODO add terminated info into reward
            terminated = th.cumsum(batch["terminated"][:, :-1].float(), dim=1)
            # set elector net state
            self.mac.elector.train()
            # train
            self.elector_optim.zero_grad()
            bs, t_total, _ = rewards.shape
            padding_sz = t_total%self.args.msg_T
            padding_sz = 0 if padding_sz ==0 else self.args.msg_T-padding_sz
            padding_reward = th.cat([rewards,
                th.zeros(bs,padding_sz,1, device=rewards.device)], dim=1)
            sum_reward = padding_reward.view(bs,
                (t_total+padding_sz)//self.args.msg_T,self.args.msg_T,1).sum(dim=2) #B,N,1 
            valid_pi=pi[:,::self.args.msg_T,:].clamp(min=1e-10)
            valid_terminated = terminated[:,::self.args.msg_T]
            sum_reward = (1-valid_terminated) * sum_reward # reward of game end is 0
            R = 0.0
            loss = 0.0
            for t in range(sum_reward.shape[1]-1, -1, -1):
                r = sum_reward[:,t,:]
                prob = valid_pi[:,t,:]
                R = r+self.args.gamma * R
                loss += -th.log(prob)*R # loss for eafch game
            # TODO: reasonable here?
            loss = loss/th.sum(1-valid_terminated, dim=1).clamp(min=1.0)
            loss = loss.mean()
            loss.backward()
            self.elector_optim.step()
            if t_env - self.log_stats_t_elector >= self.args.learner_log_interval:
                self.logger.log_stat("elector_loss", loss.item(), t_env)
                self.log_stats_t_elector = t_env
