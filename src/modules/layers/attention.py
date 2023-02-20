import torch as th
import torch.nn as nn
import torch.nn.functional as F


class EntityAttentionLayer(nn.Module):
    def __init__(self, in_dim, embed_dim, out_dim, args):
        super(EntityAttentionLayer, self).__init__()
        self.in_dim = in_dim
        self.embed_dim = embed_dim
        self.out_dim = out_dim
        self.n_heads = args.attn_n_heads
        self.n_agents = args.n_agents
        self.args = args

        assert self.embed_dim % self.n_heads == 0, "Embed dim must be divisible by n_heads"
        self.head_dim = self.embed_dim // self.n_heads
        self.register_buffer('scale_factor',
                             th.scalar_tensor(self.head_dim).sqrt())
        self.in_trans = nn.Linear(self.in_dim, self.embed_dim * 3, bias=False)
        if self.args.__dict__.get('repeat_attn', 0) > 0:
            assert in_dim == out_dim
        self.out_trans = nn.Linear(self.embed_dim, self.out_dim)

    def forward(self, entities, pre_mask=None, post_mask=None, ret_attn_logits=None, ret_attn_weights=False, rank_percent=None, entity_mask=None):
        """
        entities: Entity representations
            shape: batch size, # of entities, embedding dimension
        pre_mask: Which agent-entity pairs are not available (observability and/or padding).
                  Mask out before attention.
            shape: batch_size, # of agents, # of entities
        post_mask: Which agents/entities are not available. Zero out their outputs to
                   prevent gradients from flowing back. Shape of 2nd dim determines
                   whether to compute queries for all entities or just agents.
            shape: batch size, # of agents (or entities)
        ret_attn_logits: whether to return attention logits
            None: do not return
            "max": take max over heads
            "mean": take mean over heads
        rank_percent: leave how much percent of available entities
        entity_mask: just for calc available number of entities

        Return shape: batch size, # of agents, embedding dimension
        """
        entities_t = entities.transpose(0, 1) #ne*bs*edim
        n_queries = post_mask.shape[1] #na
        
        ne, bs, ed = entities_t.shape
        query, key, value = self.in_trans(entities_t).chunk(3, dim=2) #ne*bs*ed  * 3

        query = query[:n_queries] #na*bs*ed

        query_spl = query.reshape(n_queries, bs * self.n_heads, self.head_dim).transpose(0, 1) #(bs*n_head)*na*hd
        key_spl = key.reshape(ne, bs * self.n_heads, self.head_dim).permute(1, 2, 0) #(bs*n_head)*hd*ne
        value_spl = value.reshape(ne, bs * self.n_heads, self.head_dim).transpose(0, 1) #(bs*n_head)*ne*hd

        attn_logits = th.bmm(query_spl, key_spl) / self.scale_factor #(bs*n_head)*na*ne
        if pre_mask is not None:
            pre_mask = pre_mask[:, :n_queries] #bs*na*ne
            if pre_mask.shape[0] == bs * self.n_heads:
                pre_mask_rep = pre_mask
            else:
                pre_mask_rep = pre_mask.repeat_interleave(self.n_heads, dim=0) #(bs*n_head)*na*ne
            masked_attn_logits = attn_logits.masked_fill(pre_mask_rep[:, :, :ne].bool(), -float('Inf'))
            if rank_percent is not None:
                _, ind = masked_attn_logits.sort(2) #(bs*n_head)*na*ne
                with th.no_grad():
                    max_n = (1-entity_mask).sum(1) #bs
                    left_n = (max_n * rank_percent).ceil().long() #bs
                    arange_ind = th.arange(ne).unsqueeze(0).repeat(bs,1).to(entities_t.device) #bs, ne
                    left_ind = th.where(arange_ind>=(ne-left_n).unsqueeze(1),0,1)#bs, ne
                    expanded_left_ind = left_ind.repeat_interleave(self.n_heads,dim=0).unsqueeze(1).repeat(1,n_queries,1) #bs*nhead, na, ne
                    left_mask = th.ones_like(masked_attn_logits).flatten()
                    dk = th.arange(bs*self.n_heads*n_queries).unsqueeze(1).reshape(bs*self.n_heads,n_queries,1).to(entities_t.device)*ne
                    left_mask[(dk+ind)[expanded_left_ind==0]]=0
                    left_mask=left_mask.reshape(bs*self.n_heads, n_queries, ne) # 1 need mask, 0 remains valid. bs*nhead, na, ne
                masked_attn_logits = masked_attn_logits.masked_fill(left_mask.bool(), -float('Inf'))
                true_pre_mask = left_mask + pre_mask_rep #should be the intersection of left_mask and pre_mask_rep
                true_pre_mask[true_pre_mask>1] = 1


        attn_weights = F.softmax(masked_attn_logits, dim=2)
        # some weights might be NaN (if agent is inactive and all entities were masked)
        attn_weights = attn_weights.masked_fill(attn_weights != attn_weights, 0)
        attn_outs = th.bmm(attn_weights, value_spl) #(bs*n_head)*na*hd
        attn_outs = attn_outs.transpose(
            0, 1).reshape(n_queries, bs, self.embed_dim) #na*bs*ed
        attn_outs = attn_outs.transpose(0, 1) #bs*na*ed
        attn_outs = self.out_trans(attn_outs) #bs*na*od
        if post_mask is not None:
            attn_outs = attn_outs.masked_fill(post_mask.unsqueeze(2).bool(), 0)
        if ret_attn_logits is not None:
            # bs * n_heads, nq, ne
            attn_logits = attn_logits.reshape(bs, self.n_heads,
                                              n_queries, ne)
            if ret_attn_logits == 'max':
                attn_logits = attn_logits.max(dim=1)[0]
            elif ret_attn_logits == 'mean':
                attn_logits = attn_logits.mean(dim=1)
            elif ret_attn_logits == 'norm':
                attn_logits = attn_logits.mean(dim=1)
            return attn_outs, attn_logits
        if ret_attn_weights:
            attn_outs = (attn_outs, attn_weights)
        if rank_percent is not None:
            return attn_outs, true_pre_mask
        return attn_outs


class EntityPoolingLayer(nn.Module):
    def __init__(self, in_dim, embed_dim, out_dim, pooling_type, args):
        super(EntityPoolingLayer, self).__init__()
        self.in_dim = in_dim
        self.embed_dim = embed_dim
        self.out_dim = out_dim
        self.pooling_type = pooling_type
        self.n_agents = args.n_agents
        self.args = args

        self.in_trans = nn.Linear(self.in_dim, self.embed_dim)
        self.out_trans = nn.Linear(self.embed_dim, self.out_dim)

    def forward(self, entities, pre_mask=None, post_mask=None, ret_attn_logits=None):
        """
        entities: Entity representations
            shape: batch size, # of entities, embedding dimension
        pre_mask: Which agent-entity pairs are not available (observability and/or padding).
                  Mask out before pooling.
            shape: batch_size, # of agents, # of entities
        post_mask: Which agents are not available. Zero out their outputs to
                   prevent gradients from flowing back.
            shape: batch size, # of agents
        ret_attn_logits: not used, here to match attention layer args

        Return shape: batch size, # of agents, embedding dimension
        """
        bs, ne, ed = entities.shape

        ents_trans = self.in_trans(entities)
        n_queries = post_mask.shape[1]
        pre_mask = pre_mask[:, :n_queries]
        # duplicate all entities per agent so we can mask separately
        ents_trans_rep = ents_trans.reshape(bs, 1, ne, ed).repeat(1, self.n_agents, 1, 1)

        if pre_mask is not None:
            ents_trans_rep = ents_trans_rep.masked_fill(pre_mask.unsqueeze(3).bool(), 0)

        if self.pooling_type == 'max':
            pool_outs = ents_trans_rep.max(dim=2)[0]
        elif self.pooling_type == 'mean':
            pool_outs = ents_trans_rep.mean(dim=2)

        pool_outs = self.out_trans(pool_outs)

        if post_mask is not None:
            pool_outs = pool_outs.masked_fill(post_mask.unsqueeze(2).bool(), 0)

        if ret_attn_logits is not None:
            return pool_outs, None
        return pool_outs
