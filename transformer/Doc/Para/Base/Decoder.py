#encoding: utf-8

import torch
from torch import nn
from modules.base import *
from modules.paradoc import GateResidual
from utils.base import repeat_bsize_for_beam_tensor
from math import sqrt

from transformer.Decoder import DecoderLayer as DecoderLayerBase
from transformer.Decoder import Decoder as DecoderBase

class DecoderLayer(DecoderLayerBase):

	def __init__(self, isize, fhsize=None, dropout=0.0, attn_drop=0.0, num_head=8, ahsize=None, ncross=2):

		_ahsize = isize if ahsize is None else ahsize

		super(DecoderLayer, self).__init__(isize, fhsize, dropout, attn_drop, num_head, _ahsize)

		self.cattns = nn.ModuleList([CrossAttn(isize, _ahsize, isize, num_head, dropout=attn_drop) for i in range(ncross)])
		self.cattn_ln = nn.ModuleList([nn.LayerNorm(isize, eps=1e-06) for i in range(ncross)])
		self.grs = nn.ModuleList([GateResidual(isize) for i in range(ncross)])

	def forward(self, inpute, inputo, inputc, src_pad_mask=None, tgt_pad_mask=None, context_mask=None, query_unit=None):

		if query_unit is None:
			_inputo = self.layer_normer1(inputo)

			states_return = None

			context = self.self_attn(_inputo, mask=tgt_pad_mask)

			if self.drop is not None:
				context = self.drop(context)

			context = context + (_inputo if self.norm_residue else inputo)

		else:
			_query_unit = self.layer_normer1(query_unit)

			_inputo = _query_unit if inputo is None else torch.cat((inputo, _query_unit,), 1)

			states_return = _inputo

			context = self.self_attn(_query_unit, iK=_inputo)

			if self.drop is not None:
				context = self.drop(context)

			context = context + (_query_unit if self.norm_residue else query_unit)

		_context = self.layer_normer2(context)
		_context_new = self.cross_attn(_context, inpute, mask=src_pad_mask)

		if self.drop is not None:
			_context_new = self.drop(_context_new)

		context = _context_new + (_context if self.norm_residue else context)

		for _ln, _cattn, _gr, _inputc, _maskc in zip(self.cattn_ln, self.cattns, self.grs, inputc, [None for i in range(len(inputc))] if context_mask is None else context_mask):
			_inputs = _ln(context)
			_context = _cattn(_inputs, _inputc, mask=_maskc)
			if self.drop is not None:
				_context = self.drop(_context)
			context = _gr(_context, (_inputs if self.norm_residue else context))

		context = self.ff(context)

		if states_return is None:
			return context
		else:
			return context, states_return

	def load_base(self, base_decoder_layer):

		self.self_attn = base_decoder_layer.self_attn
		self.cross_attn = base_decoder_layer.cross_attn
		self.ff = base_decoder_layer.ff
		self.layer_normer1 = base_decoder_layer.layer_normer1
		self.layer_normer2 = base_decoder_layer.layer_normer2
		self.drop = base_decoder_layer.drop

class Decoder(DecoderBase):

	def __init__(self, isize, nwd, num_layer, fhsize=None, dropout=0.0, attn_drop=0.0, emb_w=None, num_head=8, xseql=512, ahsize=None, norm_output=True, bindemb=True, forbidden_index=None, nprev_context=2):

		_ahsize = isize if ahsize is None else ahsize

		_fhsize = _ahsize * 4 if fhsize is None else fhsize

		super(Decoder, self).__init__(isize, nwd, num_layer, _fhsize, dropout, attn_drop, emb_w, num_head, xseql, _ahsize, norm_output, bindemb, forbidden_index)

		self.nets = nn.ModuleList([DecoderLayer(isize, _fhsize, dropout, attn_drop, num_head, _ahsize, nprev_context) for i in range(num_layer)])

	def forward(self, inpute, inputo, inputc, src_pad_mask=None, context_mask=None):

		bsize, nsent, nquery = inputo.size()
		_inputo = inputo.view(-1, nquery)

		out = self.wemb(_inputo)
		isize = out.size(-1)
		out = out * sqrt(isize) + self.pemb(_inputo, expand=False)

		if self.drop is not None:
			out = self.drop(out)

		_src_pad_mask = None if src_pad_mask is None else src_pad_mask.view(-1, 1, src_pad_mask.size(-1))
		_mask = self._get_subsequent_mask(nquery)

		for net in self.nets:
			out = net(inpute, out, inputc, _src_pad_mask, _mask, context_mask)

		if self.out_normer is not None:
			out = self.out_normer(out)

		out = self.lsm(self.classifier(out)).view(bsize, nsent, nquery, -1)

		return out

	def load_base(self, base_decoder):

		self.drop = base_decoder.drop

		self.wemb = base_decoder.wemb

		self.pemb = base_decoder.pemb

		for snet, bnet in zip(self.nets, base_decoder.nets):
			snet.load_base(bnet)

		self.classifier = base_decoder.classifier

		self.lsm = base_decoder.lsm

		self.out_normer = None if self.out_normer is None else base_decoder.out_normer

	def decode(self, inpute, inputc, src_pad_mask=None, context_mask=None, beam_size=1, max_len=512, length_penalty=0.0, fill_pad=False):

		return self.beam_decode(inpute, inputc, src_pad_mask, context_mask, beam_size, max_len, length_penalty, fill_pad=fill_pad) if beam_size > 1 else self.greedy_decode(inpute, inputc, src_pad_mask, context_mask, max_len, fill_pad=fill_pad)

	def greedy_decode(self, inpute, inputc, src_pad_mask=None, context_mask=None, max_len=512, fill_pad=False):

		bsize, seql = inpute.size()[:2]

		sos_emb = self.get_sos_emb(inpute)

		sqrt_isize = sqrt(sos_emb.size(-1))

		out = sos_emb * sqrt_isize + self.pemb.get_pos(0)

		if self.drop is not None:
			out = self.drop(out)

		states = {}

		for _tmp, net in enumerate(self.nets):
			out, _state = net(inpute, None, inputc, src_pad_mask, context_mask, None, out)
			states[_tmp] = _state

		if self.out_normer is not None:
			out = self.out_normer(out)

		out = self.lsm(self.classifier(out))

		wds = out.argmax(dim=-1)

		trans = [wds]

		done_trans = wds.eq(2)

		for i in range(1, max_len):

			out = self.wemb(wds) * sqrt_isize + self.pemb.get_pos(i)

			if self.drop is not None:
				out = self.drop(out)

			for _tmp, net in enumerate(self.nets):
				out, _state = net(inpute, states[_tmp], inputc, src_pad_mask, None, context_mask, out)
				states[_tmp] = _state

			if self.out_normer is not None:
				out = self.out_normer(out)

			out = self.lsm(self.classifier(out))
			wds = out.argmax(dim=-1)

			trans.append(wds.masked_fill(done_trans, 0) if fill_pad else wds)

			done_trans = done_trans | wds.eq(2)
			if done_trans.int().sum().item() == bsize:
				break

		return torch.cat(trans, 1)

	def beam_decode(self, inpute, inputc, src_pad_mask=None, context_mask=None, beam_size=8, max_len=512, length_penalty=0.0, return_all=False, clip_beam=False, fill_pad=False):

		bsize, seql = inpute.size()[:2]

		beam_size2 = beam_size * beam_size
		bsizeb2 = bsize * beam_size2
		real_bsize = bsize * beam_size

		sos_emb = self.get_sos_emb(inpute)
		isize = sos_emb.size(-1)
		sqrt_isize = sqrt(isize)

		if length_penalty > 0.0:
			lpv = sos_emb.new_ones(real_bsize, 1)
			lpv_base = 6.0 ** length_penalty

		out = sos_emb * sqrt_isize + self.pemb.get_pos(0)

		if self.drop is not None:
			out = self.drop(out)

		states = {}

		for _tmp, net in enumerate(self.nets):
			out, _state = net(inpute, None, inputc, src_pad_mask, context_mask, None, out)
			states[_tmp] = _state

		if self.out_normer is not None:
			out = self.out_normer(out)

		out = self.lsm(self.classifier(out))

		scores, wds = out.topk(beam_size, dim=-1)
		scores = scores.squeeze(1)
		sum_scores = scores
		wds = wds.view(real_bsize, 1)
		trans = wds

		done_trans = wds.view(bsize, beam_size).eq(2)

		inpute = inpute.repeat(1, beam_size, 1).view(real_bsize, seql, isize)

		_src_pad_mask = None if src_pad_mask is None else src_pad_mask.repeat(1, beam_size, 1).view(real_bsize, 1, seql)
		_cbsize, _cseql = inputc[0].size()[:2]
		_creal_bsize = _cbsize * beam_size
		_context_mask = [None if cu is None else cu.repeat(1, beam_size, 1).view(_creal_bsize, 1, _cseql) for cu in context_mask]

		_inputc = [inputu.repeat(1, beam_size, 1).view(_creal_bsize, _cseql, isize) for inputu in inputc]

		for key, value in states.items():
			states[key] = repeat_bsize_for_beam_tensor(value, beam_size)

		for step in range(1, max_len):

			out = self.wemb(wds) * sqrt_isize + self.pemb.get_pos(step)

			if self.drop is not None:
				out = self.drop(out)

			for _tmp, net in enumerate(self.nets):
				out, _state = net(inpute, states[_tmp], _inputc, _src_pad_mask, None, _context_mask, out)
				states[_tmp] = _state

			if self.out_normer is not None:
				out = self.out_normer(out)

			out = self.lsm(self.classifier(out)).view(bsize, beam_size, -1)

			_scores, _wds = out.topk(beam_size, dim=-1)
			_scores = (_scores.masked_fill(done_trans.unsqueeze(2).expand(bsize, beam_size, beam_size), 0.0) + sum_scores.unsqueeze(2).expand(bsize, beam_size, beam_size))

			if length_penalty > 0.0:
				lpv = lpv.masked_fill(~done_trans.view(real_bsize, 1), ((step + 6.0) ** length_penalty) / lpv_base)

			if clip_beam and (length_penalty > 0.0):
				scores, _inds = (_scores.view(real_bsize, beam_size) / lpv.expand(real_bsize, beam_size)).view(bsize, beam_size2).topk(beam_size, dim=-1)
				_tinds = (_inds + torch.arange(0, bsizeb2, beam_size2, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
				sum_scores = _scores.view(bsizeb2).index_select(0, _tinds).view(bsize, beam_size)
			else:
				scores, _inds = _scores.view(bsize, beam_size2).topk(beam_size, dim=-1)
				_tinds = (_inds + torch.arange(0, bsizeb2, beam_size2, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
				sum_scores = scores

			wds = _wds.view(bsizeb2).index_select(0, _tinds).view(real_bsize, 1)

			_inds = (_inds / beam_size + torch.arange(0, real_bsize, beam_size, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)

			trans = torch.cat((trans.index_select(0, _inds), wds.masked_fill(done_trans.view(real_bsize, 1), 0) if fill_pad else wds), 1)

			done_trans = (done_trans.view(real_bsize).index_select(0, _inds) | wds.eq(2).squeeze(1)).view(bsize, beam_size)

			_done = False
			if length_penalty > 0.0:
				lpv = lpv.index_select(0, _inds)
			elif (not return_all) and done_trans.select(1, 0).int().sum().item() == bsize:
				_done = True

			if _done or (done_trans.int().sum().item() == real_bsize):
				break

			for key, value in states.items():
				states[key] = value.index_select(0, _inds)

		if (not clip_beam) and (length_penalty > 0.0):
			scores = scores / lpv.view(bsize, beam_size)
			scores, _inds = scores.topk(beam_size, dim=-1)
			_inds = (_inds + torch.arange(0, real_bsize, beam_size, dtype=_inds.dtype, device=_inds.device).unsqueeze(1).expand_as(_inds)).view(real_bsize)
			trans = trans.view(real_bsize, -1).index_select(0, _inds).view(bsize, beam_size, -1)

		if return_all:

			return trans, scores
		else:

			return trans.view(bsize, beam_size, -1).select(1, 0)