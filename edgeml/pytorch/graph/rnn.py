# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

import torch
import torch.nn as nn
from torch.autograd import Function
import numpy as np

import edgeml.pytorch.utils as utils

def onnx_exportable_fastgrnn(input, fargs, output, hidden_size, wRank, uRank, gate_nonlinearity, update_nonlinearity):
    class RNNSymbolic(Function):
        @staticmethod
        def symbolic(g, *fargs):
            # NOTE: args/kwargs contain RNN parameters
            return g.op("FastGRNN", *fargs, outputs=1,
                        hidden_size_i=hidden_size, wRank_i=wRank, uRank_i=uRank,
                        gate_nonlinearity_s=gate_nonlinearity, update_nonlinearity_s=update_nonlinearity)

        @staticmethod
        def forward(ctx, *fargs):
            return output

        @staticmethod
        def backward(ctx, *gargs, **gkwargs):
            raise RuntimeError("FIXME: Traced RNNs don't support backward")

    output_temp = RNNSymbolic.apply(input, *fargs)
    return output_temp

def gen_nonlinearity(A, nonlinearity):
    '''
    Returns required activation for a tensor based on the inputs

    nonlinearity is either a callable or a value in
        ['tanh', 'sigmoid', 'relu', 'quantTanh', 'quantSigm', 'quantSigm4']
    '''
    if nonlinearity == "tanh":
        return torch.tanh(A)
    elif nonlinearity == "sigmoid":
        return torch.sigmoid(A)
    elif nonlinearity == "relu":
        return torch.relu(A, 0.0)
    elif nonlinearity == "quantTanh":
        return torch.max(torch.min(A, torch.ones_like(A)), -1.0 * torch.ones_like(A))
    elif nonlinearity == "quantSigm":
        A = (A + 1.0) / 2.0
        return torch.max(torch.min(A, torch.ones_like(A)), torch.zeros_like(A))
    elif nonlinearity == "quantSigm4":
        A = (A + 2.0) / 4.0
        return torch.max(torch.min(A, torch.ones_like(A)), torch.zeros_like(A))
    else:
        # nonlinearity is a user specified function
        if not callable(nonlinearity):
            raise ValueError("nonlinearity is either a callable or a value " +
                             + "['tanh', 'sigmoid', 'relu', 'quantTanh', " +
                             "'quantSigm'")
        return nonlinearity(A)


class BaseRNN(nn.Module):
    '''
    Generic equivalent of static_rnn in tf
    Used to unroll all the cell written in this file
    We assume input to be batch_first by default ie.,
    [batchSize, timeSteps, inputDims] else
    [timeSteps, batchSize, inputDims]
    '''

    def __init__(self, RNNCell, batch_first=True):
        super(BaseRNN, self).__init__()
        self._RNNCell = RNNCell
        self._batch_first = batch_first

    def getVars(self):
        return self._RNNCell.getVars()

    def forward(self, input, hiddenState=None,
                cellState=None):
        if self._batch_first is True:
            self.device = input.device
            hiddenStates = torch.zeros(
                [input.shape[0], input.shape[1],
                 self._RNNCell.output_size]).to(self.device)
            if hiddenState is None:
                hiddenState = torch.zeros([input.shape[0],
                                           self._RNNCell.output_size]).to(self.device)
            if self._RNNCell.cellType == "LSTMLR":
                cellStates = torch.zeros(
                    [input.shape[0], input.shape[1],
                     self._RNNCell.output_size]).to(self.device)
                if cellState is None:
                    cellState = torch.zeros(
                        [input.shape[0], self._RNNCell.output_size]).to(self.device)
                for i in range(0, input.shape[1]):
                    hiddenState, cellState = self._RNNCell(
                        input[:, i, :], (hiddenState, cellState))
                    hiddenStates[:, i, :] = hiddenState
                    cellStates[:, i, :] = cellState
                return hiddenStates, cellStates
            else:
                for i in range(0, input.shape[1]):
                    hiddenState = self._RNNCell(input[:, i, :], hiddenState)
                    hiddenStates[:, i, :] = hiddenState
                return hiddenStates
        else:
            self.device = input.device
            hiddenStates = torch.zeros(
                [input.shape[0], input.shape[1],
                 self._RNNCell.output_size]).to(self.device)
            if hiddenState is None:
                hiddenState = torch.zeros([input.shape[1],
                                           self._RNNCell.output_size]).to(self.device)
            if self._RNNCell.cellType == "LSTMLR":
                cellStates = torch.zeros(
                    [input.shape[0], input.shape[1],
                     self._RNNCell.output_size]).to(self.device)
                if cellState is None:
                    cellState = torch.zeros(
                        [input.shape[1], self._RNNCell.output_size]).to(self.device)
                for i in range(0, input.shape[0]):
                    hiddenState, cellState = self._RNNCell(
                        input[i, :, :], (hiddenState, cellState))
                    hiddenStates[i, :, :] = hiddenState
                    cellStates[i, :, :] = cellState
                return hiddenStates, cellStates
            else:
                for i in range(0, input.shape[0]):
                    hiddenState = self._RNNCell(input[i, :, :], hiddenState)
                    hiddenStates[i, :, :] = hiddenState
                return hiddenStates


class FastGRNNCell(nn.Module):
    '''
    FastGRNN Cell with Both Full Rank and Low Rank Formulations
    Has multiple activation functions for the gates
    hidden_size = # hidden units

    gate_nonlinearity = nonlinearity for the gate can be chosen from
    [tanh, sigmoid, relu, quantTanh, quantSigm]
    update_nonlinearity = nonlinearity for final rnn update
    can be chosen from [tanh, sigmoid, relu, quantTanh, quantSigm]

    wRank = rank of W matrix (creates two matrices if not None)
    uRank = rank of U matrix (creates two matrices if not None)
    
    wSparsity = intended sparsity of W matrix(ces)
    uSparsity = intended sparsity of U matrix(ces)
    Warning:
    The Cell will not automatically sparsify.
    The user must invoke .sparsify to hard threshold.

    zetaInit = init for zeta, the scale param
    nuInit = init for nu, the translation param

    FastGRNN architecture and compression techniques are found in
    FastGRNN(LINK) paper

    Basic architecture is like:

    z_t = gate_nl(Wx_t + Uh_{t-1} + B_g)
    h_t^ = update_nl(Wx_t + Uh_{t-1} + B_h)
    h_t = z_t*h_{t-1} + (sigmoid(zeta)(1-z_t) + sigmoid(nu))*h_t^

    W and U can further parameterised into low rank version by
    W = matmul(W_1, W_2) and U = matmul(U_1, U_2)
    '''

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None,
                 wSparsity=1.0, uSparsity=1.0, zetaInit=1.0, nuInit=-4.0,
                 name="FastGRNN"):
        super(FastGRNNCell, self).__init__()

        self._input_size = input_size
        self._hidden_size = hidden_size
        self._gate_nonlinearity = gate_nonlinearity
        self._update_nonlinearity = update_nonlinearity
        self._num_weight_matrices = [1, 1]
        self._wRank = wRank
        self._uRank = uRank
        self._wSparsity = wSparsity 
        self._uSparsity = uSparsity
        self._zetaInit = zetaInit
        self._nuInit = nuInit
        if wRank is not None:
            self._num_weight_matrices[0] += 1
        if uRank is not None:
            self._num_weight_matrices[1] += 1
        self._name = name

        if wRank is None:
            self.W = nn.Parameter(0.1 * torch.randn([hidden_size, input_size]))
        else:
            self.W1 = nn.Parameter(0.1 * torch.randn([wRank, input_size]))
            self.W2 = nn.Parameter(0.1 * torch.randn([hidden_size, wRank]))

        if uRank is None:
            self.U = nn.Parameter(0.1 * torch.randn([hidden_size, hidden_size]))
        else:
            self.U1 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))
            self.U2 = nn.Parameter(0.1 * torch.randn([hidden_size, uRank]))
        
        self.copy_previous_state()
          
        self.bias_gate = nn.Parameter(torch.ones([1, hidden_size]))
        self.bias_update = nn.Parameter(torch.ones([1, hidden_size]))
        self.zeta = nn.Parameter(self._zetaInit * torch.ones([1, 1]))
        self.nu = nn.Parameter(self._nuInit * torch.ones([1, 1]))

    @property
    def state_size(self):
        return self._hidden_size

    @property
    def input_size(self):
        return self._input_size

    @property
    def output_size(self):
        return self._hidden_size

    @property
    def gate_nonlinearity(self):
        return self._gate_nonlinearity

    @property
    def update_nonlinearity(self):
        return self._update_nonlinearity

    @property
    def wRank(self):
        return self._wRank

    @property
    def uRank(self):
        return self._uRank

    @property
    def num_weight_matrices(self):
        return self._num_weight_matrices

    @property
    def name(self):
        return self._name

    @property
    def cellType(self):
        return "FastGRNN"

    def forward(self, input, state):
        if self._wRank is None:
            wComp = torch.matmul(input, torch.transpose(self.W, 0, 1))
        else:
            wComp = torch.matmul(
                torch.matmul(input, torch.transpose(self.W1, 0, 1)), torch.transpose(self.W2, 0, 1))

        if self._uRank is None:
            uComp = torch.matmul(state, torch.transpose(self.U, 0, 1))
        else:
            uComp = torch.matmul(
                torch.matmul(state, torch.transpose(self.U1, 0, 1)), torch.transpose(self.U2, 0, 1))

        pre_comp = wComp + uComp

        z = gen_nonlinearity(pre_comp + self.bias_gate,
                              self._gate_nonlinearity)
        c = gen_nonlinearity(pre_comp + self.bias_update,
                              self._update_nonlinearity)
        new_h = z * state + (torch.sigmoid(self.zeta) *
                             (1.0 - z) + torch.sigmoid(self.nu)) * c

        return new_h

    def getVars(self):
        Vars = []
        if self._num_weight_matrices[0] == 1:
            Vars.append(self.W)
        else:
            Vars.extend([self.W1, self.W2])

        if self._num_weight_matrices[1] == 1:
            Vars.append(self.U)
        else:
            Vars.extend([self.U1, self.U2])

        Vars.extend([self.bias_gate, self.bias_update])
        Vars.extend([self.zeta, self.nu])
        return Vars

    def getModelSize(self):
        '''
		Function to get aimed model size
		'''
        totalnnz = 2  # For Zeta and Nu

        mats = self.getVars()

        endW = self._num_weight_matrices[0]
        for i in range(0, endW):
            totalnnz += utils.countNNZ(mats[i], self._wSparsity)

        endU = endW + self._num_weight_matrices[1]
        for i in range(endW, endU):
            totalnnz += utils.countNNZ(mats[i], self._uSparsity)

        for i in range(endU, mats.len()):
            totalnnz += utils.countNNZ(mats[i], False)

        return totalnnz

        #totalnnz += utils.countNNZ(self.bias_gate, False)
        #totalnnz += utils.countNNZ(self.bias_update, False)
        #if self._wRank is None:
        #    totalnnz += utils.countNNZ(self.W, self._wSparsity)
        #else:
        #    totalnnz += utils.countNNZ(self.W1, self._wSparsity)
        #    totalnnz += utils.countNNZ(self.W2, self._wSparsity)

        #if self._uRank is None:
        #    totalnnz += utils.countNNZ(self.U, self._uSparsity)
        #else:
        #    totalnnz += utils.countNNZ(self.U1, self._uSparsity)
        #    totalnnz += utils.countNNZ(self.U2, self._uSparsity)

    
    def copy_previous_state(self):
        if self._wRank is None:
            if self._wSparsity < 1.0:
                self.W_old = torch.FloatTensor(np.copy(self.W.data.cpu().detach().numpy()))
                self.W_old.to(self.W.device)
        else:
            if self._wSparsity < 1.0:
                self.W1_old = torch.FloatTensor(np.copy(self.W1.data.cpu().detach().numpy()))
                self.W2_old = torch.FloatTensor(np.copy(self.W2.data.cpu().detach().numpy()))
                self.W1_old.to(self.W1.device)
                self.W2_old.to(self.W2.device)

        if self._uRank is None:
            if self._uSparsity < 1.0:
                self.U_old = torch.FloatTensor(np.copy(self.U.data.cpu().detach().numpy()))
                self.U_old.to(self.U.device)
        else:
            if self._uSparsity < 1.0:
                self.U1_old = torch.FloatTensor(np.copy(self.U1.data.cpu().detach().numpy()))
                self.U2_old = torch.FloatTensor(np.copy(self.U2.data.cpu().detach().numpy()))
                self.U1_old.to(self.U1.device)
                self.U2_old.to(self.U2.device)
        
    def sparsify(self):
        if self._wRank is None:
            self.W.data = utils.hardThreshold(self.W, self._wSparsity)
        else:
            self.W1.data = utils.hardThreshold(self.W1, self._wSparsity)
            self.W2.data = utils.hardThreshold(self.W2, self._wSparsity)

        if self._uRank is None:
            self.U.data = utils.hardThreshold(self.U, self._uSparsity)
        else:
            self.U1.data = utils.hardThreshold(self.U1, self._uSparsity)
            self.U2.data = utils.hardThreshold(self.U2, self._uSparsity)
        self.copy_previous_state()

    def sparsifyWithSupport(self):
        if self._wRank is None:
            self.W.data = utils.supportBasedThreshold(self.W, self.W_old)
        else:
            self.W1.data = utils.supportBasedThreshold(self.W1, self.W1_old)
            self.W2.data = utils.supportBasedThreshold(self.W2, self.W2_old)

        if self._uRank is None:
            self.U.data = utils.supportBasedThreshold(self.U, self.U_old)
        else:
            self.U1.data = utils.supportBasedThreshold(self.U1, self.U1_old)
            self.U2.data = utils.supportBasedThreshold(self.U2, self.U2_old)
        #self.copy_previous_state()


class FastRNNCell(nn.Module):
    '''
    FastRNN Cell with Both Full Rank and Low Rank Formulations
    Has multiple activation functions for the gates
    hidden_size = # hidden units

    update_nonlinearity = nonlinearity for final rnn update
    can be chosen from [tanh, sigmoid, relu, quantTanh, quantSigm]

    wRank = rank of W matrix (creates two matrices if not None)
    uRank = rank of U matrix (creates two matrices if not None)
     
    wSparsity = intended sparsity of W matrix(ces)
    uSparsity = intended sparsity of U matrix(ces)
    Warning:
    The Cell will not automatically sparsify.
    The user must invoke .sparsify to hard threshold.

    alphaInit = init for alpha, the update scalar
    betaInit = init for beta, the weight for previous state

    FastRNN architecture and compression techniques are found in
    FastGRNN(LINK) paper

    Basic architecture is like:

    h_t^ = update_nl(Wx_t + Uh_{t-1} + B_h)
    h_t = sigmoid(beta)*h_{t-1} + sigmoid(alpha)*h_t^

    W and U can further parameterised into low rank version by
    W = matmul(W_1, W_2) and U = matmul(U_1, U_2)
    '''

    def __init__(self, input_size, hidden_size,
                 update_nonlinearity="tanh", wRank=None, uRank=None,
                 wSparsity=1.0, uSparsity=1.0, alphaInit=-3.0, betaInit=3.0,
                 name="FastRNN"):
        super(FastRNNCell, self).__init__()

        self._input_size = input_size
        self._hidden_size = hidden_size
        self._update_nonlinearity = update_nonlinearity
        self._num_weight_matrices = [1, 1]
        self._wRank = wRank
        self._uRank = uRank
        self._wSparsity = wSparsity 
        self._uSparsity = uSparsity
        self._alphaInit = alphaInit
        self._betaInit = betaInit
        if wRank is not None:
            self._num_weight_matrices[0] += 1
        if uRank is not None:
            self._num_weight_matrices[1] += 1
        self._name = name

        if wRank is None:
            self.W = nn.Parameter(0.1 * torch.randn([input_size, hidden_size]))
        else:
            self.W1 = nn.Parameter(0.1 * torch.randn([input_size, wRank]))
            self.W2 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))

        if uRank is None:
            self.U = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
        else:
            self.U1 = nn.Parameter(0.1 * torch.randn([hidden_size, uRank]))
            self.U2 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))

        self.bias_update = nn.Parameter(torch.ones([1, hidden_size]))
        self.alpha = nn.Parameter(self._alphaInit * torch.ones([1, 1]))
        self.beta = nn.Parameter(self._betaInit * torch.ones([1, 1]))

    def sparsify(self):
        if self._wRank is None:
            self.W.data = utils.hardThreshold(self.W, self._wSparsity)
        else:
            self.W1.data = utils.hardThreshold(self.W1, self._wSparsity)
            self.W2.data = utils.hardThreshold(self.W2, self._wSparsity)

        if self._uRank is None:
            self.U.data = utils.hardThreshold(self.U, self._uSparsity)
        else:
            self.U1.data = utils.hardThreshold(self.U1, self._uSparsity)
            self.U2.data = utils.hardThreshold(self.U2, self._uSparsity)


    @property
    def state_size(self):
        return self._hidden_size

    @property
    def input_size(self):
        return self._input_size

    @property
    def output_size(self):
        return self._hidden_size

    @property
    def update_nonlinearity(self):
        return self._update_nonlinearity

    @property
    def wRank(self):
        return self._wRank

    @property
    def uRank(self):
        return self._uRank

    @property
    def num_weight_matrices(self):
        return self._num_weight_matrices

    @property
    def name(self):
        return self._name

    @property
    def cellType(self):
        return "FastRNN"

    def forward(self, input, state):
        if self._wRank is None:
            wComp = torch.matmul(input, self.W)
        else:
            wComp = torch.matmul(
                torch.matmul(input, self.W1), self.W2)

        if self._uRank is None:
            uComp = torch.matmul(state, self.U)
        else:
            uComp = torch.matmul(
                torch.matmul(state, self.U1), self.U2)

        pre_comp = wComp + uComp

        c = gen_nonlinearity(pre_comp + self.bias_update,
                              self._update_nonlinearity)
        new_h = torch.sigmoid(self.beta) * state + \
            torch.sigmoid(self.alpha) * c

        return new_h

    def getVars(self):
        Vars = []
        if self._num_weight_matrices[0] == 1:
            Vars.append(self.W)
        else:
            Vars.extend([self.W1, self.W2])

        if self._num_weight_matrices[1] == 1:
            Vars.append(self.U)
        else:
            Vars.extend([self.U1, self.U2])

        Vars.extend([self.bias_update])
        Vars.extend([self.alpha, self.beta])

        return Vars

    def getModelSize(self):
        '''
		Function to get aimed model size
		'''
        totalnnz = 2  # For \alpha and \beta
        totalnnz += utils.countNNZ(self.bias_update, False)
        if self._wRank is None:
            totalnnz += utils.countNNZ(self.W, self._wSparsity)
        else:
            totalnnz += utils.countNNZ(self.W1, self._wSparsity)
            totalnnz += utils.countNNZ(self.W2, self._wSparsity)

        if self._uRank is None:
            totalnnz += utils.countNNZ(self.U, self._uSparsity)
        else:
            totalnnz += utils.countNNZ(self.U1, self._uSparsity)
            totalnnz += utils.countNNZ(self.U2, self._uSparsity)
        return totalnnz

class LSTMLRCell(nn.Module):
    '''
    LR - Low Rank
    LSTM LR Cell with Both Full Rank and Low Rank Formulations
    Has multiple activation functions for the gates
    hidden_size = # hidden units

    gate_nonlinearity = nonlinearity for the gate can be chosen from
    [tanh, sigmoid, relu, quantTanh, quantSigm]
    update_nonlinearity = nonlinearity for final rnn update
    can be chosen from [tanh, sigmoid, relu, quantTanh, quantSigm]

    wRank = rank of all W matrices
    (creates 5 matrices if not None else creates 4 matrices)
    uRank = rank of all U matrices
    (creates 5 matrices if not None else creates 4 matrices)

    LSTM architecture and compression techniques are found in
    LSTM paper

    Basic architecture:

    f_t = gate_nl(W1x_t + U1h_{t-1} + B_f)
    i_t = gate_nl(W2x_t + U2h_{t-1} + B_i)
    C_t^ = update_nl(W3x_t + U3h_{t-1} + B_c)
    o_t = gate_nl(W4x_t + U4h_{t-1} + B_o)
    C_t = f_t*C_{t-1} + i_t*C_t^
    h_t = o_t*update_nl(C_t)

    Wi and Ui can further parameterised into low rank version by
    Wi = matmul(W, W_i) and Ui = matmul(U, U_i)
    '''

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None,
                 name="LSTMLR"):
        super(LSTMLRCell, self).__init__()

        self._input_size = input_size
        self._hidden_size = hidden_size
        self._gate_nonlinearity = gate_nonlinearity
        self._update_nonlinearity = update_nonlinearity
        self._num_weight_matrices = [4, 4]
        self._wRank = wRank
        self._uRank = uRank
        if wRank is not None:
            self._num_weight_matrices[0] += 1
        if uRank is not None:
            self._num_weight_matrices[1] += 1
        self._name = name

        if wRank is None:
            self.W1 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
            self.W2 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
            self.W3 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
            self.W4 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
        else:
            self.W = nn.Parameter(0.1 * torch.randn([input_size, wRank]))
            self.W1 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))
            self.W2 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))
            self.W3 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))
            self.W4 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))

        if uRank is None:
            self.U1 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
            self.U2 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
            self.U3 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
            self.U4 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
        else:
            self.U = nn.Parameter(0.1 * torch.randn([hidden_size, uRank]))
            self.U1 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))
            self.U2 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))
            self.U3 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))
            self.U4 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))

        self.bias_f = nn.Parameter(torch.ones([1, hidden_size]))
        self.bias_i = nn.Parameter(torch.ones([1, hidden_size]))
        self.bias_c = nn.Parameter(torch.ones([1, hidden_size]))
        self.bias_o = nn.Parameter(torch.ones([1, hidden_size]))

    @property
    def state_size(self):
        return 2 * self._hidden_size

    @property
    def input_size(self):
        return self._input_size

    @property
    def output_size(self):
        return self._hidden_size

    @property
    def gate_nonlinearity(self):
        return self._gate_nonlinearity

    @property
    def update_nonlinearity(self):
        return self._update_nonlinearity

    @property
    def wRank(self):
        return self._wRank

    @property
    def uRank(self):
        return self._uRank

    @property
    def num_weight_matrices(self):
        return self._num_weight_matrices

    @property
    def name(self):
        return self._name

    @property
    def cellType(self):
        return "LSTMLR"

    def forward(self, input, hiddenStates):
        (h, c) = hiddenStates

        if self._wRank is None:
            wComp1 = torch.matmul(input, self.W1)
            wComp2 = torch.matmul(input, self.W2)
            wComp3 = torch.matmul(input, self.W3)
            wComp4 = torch.matmul(input, self.W4)
        else:
            wComp1 = torch.matmul(
                torch.matmul(input, self.W), self.W1)
            wComp2 = torch.matmul(
                torch.matmul(input, self.W), self.W2)
            wComp3 = torch.matmul(
                torch.matmul(input, self.W), self.W3)
            wComp4 = torch.matmul(
                torch.matmul(input, self.W), self.W4)

        if self._uRank is None:
            uComp1 = torch.matmul(h, self.U1)
            uComp2 = torch.matmul(h, self.U2)
            uComp3 = torch.matmul(h, self.U3)
            uComp4 = torch.matmul(h, self.U4)
        else:
            uComp1 = torch.matmul(
                torch.matmul(h, self.U), self.U1)
            uComp2 = torch.matmul(
                torch.matmul(h, self.U), self.U2)
            uComp3 = torch.matmul(
                torch.matmul(h, self.U), self.U3)
            uComp4 = torch.matmul(
                torch.matmul(h, self.U), self.U4)
        pre_comp1 = wComp1 + uComp1
        pre_comp2 = wComp2 + uComp2
        pre_comp3 = wComp3 + uComp3
        pre_comp4 = wComp4 + uComp4

        i = gen_nonlinearity(pre_comp1 + self.bias_i,
                              self._gate_nonlinearity)
        f = gen_nonlinearity(pre_comp2 + self.bias_f,
                              self._gate_nonlinearity)
        o = gen_nonlinearity(pre_comp4 + self.bias_o,
                              self._gate_nonlinearity)

        c_ = gen_nonlinearity(pre_comp3 + self.bias_c,
                               self._update_nonlinearity)

        new_c = f * c + i * c_
        new_h = o * gen_nonlinearity(new_c, self._update_nonlinearity)
        return new_h, new_c

    def getVars(self):
        Vars = []
        if self._num_weight_matrices[0] == 4:
            Vars.extend([self.W1, self.W2, self.W3, self.W4])
        else:
            Vars.extend([self.W, self.W1, self.W2, self.W3, self.W4])

        if self._num_weight_matrices[1] == 4:
            Vars.extend([self.U1, self.U2, self.U3, self.U4])
        else:
            Vars.extend([self.U, self.U1, self.U2, self.U3, self.U4])

        Vars.extend([self.bias_f, self.bias_i, self.bias_c, self.bias_o])

        return Vars


class GRULRCell(nn.Module):
    '''
    GRU LR Cell with Both Full Rank and Low Rank Formulations
    Has multiple activation functions for the gates
    hidden_size = # hidden units

    gate_nonlinearity = nonlinearity for the gate can be chosen from
    [tanh, sigmoid, relu, quantTanh, quantSigm]
    update_nonlinearity = nonlinearity for final rnn update
    can be chosen from [tanh, sigmoid, relu, quantTanh, quantSigm]

    wRank = rank of W matrix
    (creates 4 matrices if not None else creates 3 matrices)
    uRank = rank of U matrix
    (creates 4 matrices if not None else creates 3 matrices)

    GRU architecture and compression techniques are found in
    GRU(LINK) paper

    Basic architecture is like:

    r_t = gate_nl(W1x_t + U1h_{t-1} + B_r)
    z_t = gate_nl(W2x_t + U2h_{t-1} + B_g)
    h_t^ = update_nl(W3x_t + r_t*U3(h_{t-1}) + B_h)
    h_t = z_t*h_{t-1} + (1-z_t)*h_t^

    Wi and Ui can further parameterised into low rank version by
    Wi = matmul(W, W_i) and Ui = matmul(U, U_i)
    '''

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None,
                 name="GRULR"):
        super(GRULRCell, self).__init__()

        self._input_size = input_size
        self._hidden_size = hidden_size
        self._gate_nonlinearity = gate_nonlinearity
        self._update_nonlinearity = update_nonlinearity
        self._num_weight_matrices = [3, 3]
        self._wRank = wRank
        self._uRank = uRank
        if wRank is not None:
            self._num_weight_matrices[0] += 1
        if uRank is not None:
            self._num_weight_matrices[1] += 1
        self._name = name

        if wRank is None:
            self.W1 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
            self.W2 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
            self.W3 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
        else:
            self.W = nn.Parameter(0.1 * torch.randn([input_size, wRank]))
            self.W1 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))
            self.W2 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))
            self.W3 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))

        if uRank is None:
            self.U1 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
            self.U2 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
            self.U3 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
        else:
            self.U = nn.Parameter(0.1 * torch.randn([hidden_size, uRank]))
            self.U1 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))
            self.U2 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))
            self.U3 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))

        self.bias_r = nn.Parameter(torch.ones([1, hidden_size]))
        self.bias_gate = nn.Parameter(torch.ones([1, hidden_size]))
        self.bias_update = nn.Parameter(torch.ones([1, hidden_size]))
        self._device = self.bias_update.device

    @property
    def state_size(self):
        return self._hidden_size

    @property
    def input_size(self):
        return self._input_size

    @property
    def output_size(self):
        return self._hidden_size

    @property
    def gate_nonlinearity(self):
        return self._gate_nonlinearity

    @property
    def update_nonlinearity(self):
        return self._update_nonlinearity

    @property
    def wRank(self):
        return self._wRank

    @property
    def uRank(self):
        return self._uRank

    @property
    def num_weight_matrices(self):
        return self._num_weight_matrices

    @property
    def name(self):
        return self._name

    @property
    def cellType(self):
        return "GRULR"

    def forward(self, input, state):
        if self._wRank is None:
            wComp1 = torch.matmul(input, self.W1)
            wComp2 = torch.matmul(input, self.W2)
            wComp3 = torch.matmul(input, self.W3)
        else:
            wComp1 = torch.matmul(
                torch.matmul(input, self.W), self.W1)
            wComp2 = torch.matmul(
                torch.matmul(input, self.W), self.W2)
            wComp3 = torch.matmul(
                torch.matmul(input, self.W), self.W3)

        if self._uRank is None:
            uComp1 = torch.matmul(state, self.U1)
            uComp2 = torch.matmul(state, self.U2)
        else:
            uComp1 = torch.matmul(
                torch.matmul(state, self.U), self.U1)
            uComp2 = torch.matmul(
                torch.matmul(state, self.U), self.U2)

        pre_comp1 = wComp1 + uComp1
        pre_comp2 = wComp2 + uComp2

        r = gen_nonlinearity(pre_comp1 + self.bias_r,
                              self._gate_nonlinearity)
        z = gen_nonlinearity(pre_comp2 + self.bias_gate,
                              self._gate_nonlinearity)

        if self._uRank is None:
            pre_comp3 = wComp3 + torch.matmul(r * state, self.U3)
        else:
            pre_comp3 = wComp3 + \
                torch.matmul(torch.matmul(r * state, self.U), self.U3)

        c = gen_nonlinearity(pre_comp3 + self.bias_update,
                              self._update_nonlinearity)

        new_h = z * state + (1.0 - z) * c
        return new_h

    def getVars(self):
        Vars = []
        if self._num_weight_matrices[0] == 3:
            Vars.extend([self.W1, self.W2, self.W3])
        else:
            Vars.extend([self.W, self.W1, self.W2, self.W3])

        if self._num_weight_matrices[1] == 3:
            Vars.extend([self.U1, self.U2, self.U3])
        else:
            Vars.extend([self.U, self.U1, self.U2, self.U3])

        Vars.extend([self.bias_r, self.bias_gate, self.bias_update])

        return Vars


class UGRNNLRCell(nn.Module):
    '''
    UGRNN LR Cell with Both Full Rank and Low Rank Formulations
    Has multiple activation functions for the gates
    hidden_size = # hidden units

    gate_nonlinearity = nonlinearity for the gate can be chosen from
    [tanh, sigmoid, relu, quantTanh, quantSigm]
    update_nonlinearity = nonlinearity for final rnn update
    can be chosen from [tanh, sigmoid, relu, quantTanh, quantSigm]

    wRank = rank of W matrix
    (creates 3 matrices if not None else creates 2 matrices)
    uRank = rank of U matrix
    (creates 3 matrices if not None else creates 2 matrices)

    UGRNN architecture and compression techniques are found in
    UGRNN(LINK) paper

    Basic architecture is like:

    z_t = gate_nl(W1x_t + U1h_{t-1} + B_g)
    h_t^ = update_nl(W1x_t + U1h_{t-1} + B_h)
    h_t = z_t*h_{t-1} + (1-z_t)*h_t^

    Wi and Ui can further parameterised into low rank version by
    Wi = matmul(W, W_i) and Ui = matmul(U, U_i)
    '''

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None,
                 name="UGRNNLR"):
        super(UGRNNLRCell, self).__init__()

        self._input_size = input_size
        self._hidden_size = hidden_size
        self._gate_nonlinearity = gate_nonlinearity
        self._update_nonlinearity = update_nonlinearity
        self._num_weight_matrices = [2, 2]
        self._wRank = wRank
        self._uRank = uRank
        if wRank is not None:
            self._num_weight_matrices[0] += 1
        if uRank is not None:
            self._num_weight_matrices[1] += 1
        self._name = name

        if wRank is None:
            self.W1 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
            self.W2 = nn.Parameter(
                0.1 * torch.randn([input_size, hidden_size]))
        else:
            self.W = nn.Parameter(0.1 * torch.randn([input_size, wRank]))
            self.W1 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))
            self.W2 = nn.Parameter(0.1 * torch.randn([wRank, hidden_size]))

        if uRank is None:
            self.U1 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
            self.U2 = nn.Parameter(
                0.1 * torch.randn([hidden_size, hidden_size]))
        else:
            self.U = nn.Parameter(0.1 * torch.randn([hidden_size, uRank]))
            self.U1 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))
            self.U2 = nn.Parameter(0.1 * torch.randn([uRank, hidden_size]))

        self.bias_gate = nn.Parameter(torch.ones([1, hidden_size]))
        self.bias_update = nn.Parameter(torch.ones([1, hidden_size]))
        self._device = self.bias_update.device

    @property
    def state_size(self):
        return self._hidden_size

    @property
    def input_size(self):
        return self._input_size

    @property
    def output_size(self):
        return self._hidden_size

    @property
    def gate_nonlinearity(self):
        return self._gate_nonlinearity

    @property
    def update_nonlinearity(self):
        return self._update_nonlinearity

    @property
    def wRank(self):
        return self._wRank

    @property
    def uRank(self):
        return self._uRank

    @property
    def num_weight_matrices(self):
        return self._num_weight_matrices

    @property
    def name(self):
        return self._name

    @property
    def cellType(self):
        return "UGRNNLR"

    def forward(self, input, state):
        if self._wRank is None:
            wComp1 = torch.matmul(input, self.W1)
            wComp2 = torch.matmul(input, self.W2)
        else:
            wComp1 = torch.matmul(
                torch.matmul(input, self.W), self.W1)
            wComp2 = torch.matmul(
                torch.matmul(input, self.W), self.W2)

        if self._uRank is None:
            uComp1 = torch.matmul(state, self.U1)
            uComp2 = torch.matmul(state, self.U2)
        else:
            uComp1 = torch.matmul(
                torch.matmul(state, self.U), self.U1)
            uComp2 = torch.matmul(
                torch.matmul(state, self.U), self.U2)

        pre_comp1 = wComp1 + uComp1
        pre_comp2 = wComp2 + uComp2

        z = gen_nonlinearity(pre_comp1 + self.bias_gate,
                              self._gate_nonlinearity)
        c = gen_nonlinearity(pre_comp2 + self.bias_update,
                              self._update_nonlinearity)

        new_h = z * state + (1.0 - z) * c
        return new_h

    def getVars(self):
        Vars = []
        if self._num_weight_matrices[0] == 2:
            Vars.extend([self.W1, self.W2])
        else:
            Vars.extend([self.W, self.W1, self.W2])

        if self._num_weight_matrices[1] == 2:
            Vars.extend([self.U1, self.U2])
        else:
            Vars.extend([self.U, self.U1, self.U2])

        Vars.extend([self.bias_gate, self.bias_update])

        return Vars


class LSTM(nn.Module):
    """Equivalent to nn.LSTM using LSTMLRCell"""

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None, batch_first=True):
        super(LSTM, self).__init__()
        self.cell = LSTMLRCell(input_size, hidden_size,
                               gate_nonlinearity=gate_nonlinearity,
                               update_nonlinearity=update_nonlinearity,
                               wRank=wRank, uRank=uRank)
        self.unrollRNN = BaseRNN(self.cell, batch_first=batch_first)

    def forward(self, input, hiddenState=None, cellState=None):
        return self.unrollRNN(input, hiddenState, cellState)


class GRU(nn.Module):
    """Equivalent to nn.GRU using GRULRCell"""

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None, batch_first=True):
        super(GRU, self).__init__()
        self.cell = GRULRCell(input_size, hidden_size,
                              gate_nonlinearity=gate_nonlinearity,
                              update_nonlinearity=update_nonlinearity,
                              wRank=wRank, uRank=uRank)
        self.unrollRNN = BaseRNN(self.cell, batch_first=batch_first)

    def forward(self, input, hiddenState=None, cellState=None):
        return self.unrollRNN(input, hiddenState, cellState)


class UGRNN(nn.Module):
    """Equivalent to nn.UGRNN using UGRNNLRCell"""

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None, batch_first=True):
        super(UGRNN, self).__init__()
        self.cell = UGRNNLRCell(input_size, hidden_size,
                                gate_nonlinearity=gate_nonlinearity,
                                update_nonlinearity=update_nonlinearity,
                                wRank=wRank, uRank=uRank)
        self.unrollRNN = BaseRNN(self.cell, batch_first=batch_first)

    def forward(self, input, hiddenState=None, cellState=None):
        return self.unrollRNN(input, hiddenState, cellState)


class FastRNN(nn.Module):
    """Equivalent to nn.FastRNN using FastRNNCell"""

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None,
                 alphaInit=-3.0, betaInit=3.0, batch_first=True):
        super(FastRNN, self).__init__()
        self.cell = FastRNNCell(input_size, hidden_size,
                                gate_nonlinearity=gate_nonlinearity,
                                update_nonlinearity=update_nonlinearity,
                                wRank=wRank, uRank=uRank,
                                alphaInit=alphaInit, betaInit=betaInit)
        self.unrollRNN = BaseRNN(self.cell, batch_first=batch_first)

    def forward(self, input, hiddenState=None, cellState=None):
        return self.unrollRNN(input, hiddenState, cellState)


class FastGRNN(nn.Module):
    """Equivalent to nn.FastGRNN using FastGRNNCell"""

    def __init__(self, input_size, hidden_size, gate_nonlinearity="sigmoid",
                 update_nonlinearity="tanh", wRank=None, uRank=None,
                 wSparsity=1.0, uSparsity=1.0, zetaInit=1.0, nuInit=-4.0,
                 batch_first=True):
        super(FastGRNN, self).__init__()
        self.cell = FastGRNNCell(input_size, hidden_size,
                                 gate_nonlinearity=gate_nonlinearity,
                                 update_nonlinearity=update_nonlinearity,
                                 wRank=wRank, uRank=uRank, 
                                 wSparsity=wSparsity, uSparsity=uSparsity, 
                                 zetaInit=zetaInit, nuInit=nuInit)
        self.unrollRNN = BaseRNN(self.cell, batch_first=batch_first)

    def getVars(self):
        return self.unrollRNN.getVars()

    def forward(self, input, hiddenState=None, cellState=None):
        return self.unrollRNN(input, hiddenState, cellState)

class SRNN2(nn.Module):

    def __init__(self, inputDim, outputDim, hiddenDim0, hiddenDim1, cellType):
        '''
        A 2 Layer Shallow RNN.

        inputDim: Input data's feature dimension.
        hiddenDim0: Hidden state dimension of the lower layer RNN cell.
        hiddenDim1: Hidden state dimension of the second layer RNN cell.
        cellType: The type of RNN cell to use. Options are ['LSTM']
        '''
        super(SRNN2, self).__init__()
        # Create two RNN Cells
        self.inputDim = inputDim
        self.hiddenDim0 = hiddenDim0
        self.hiddenDim1 = hiddenDim1
        self.outputDim = outputDim
        supportedCells = ['LSTM']
        assert cellType in supportedCells, 'Currently supported cells: %r' % supportedCells
        self.cellType = cellType
        if self.cellType == 'LSTM':
            self.rnnClass = nn.LSTM

        self.rnn0 = self.rnnClass(input_size=inputDim, hidden_size=hiddenDim0)
        self.rnn1 = self.rnnClass(input_size=hiddenDim0, hidden_size=hiddenDim1)
        self.W = torch.randn([self.hiddenDim1, self.outputDim])
        self.W = nn.Parameter(self.W)
        self.B = torch.randn([self.outputDim])
        self.B = nn.Parameter(self.B)

    def getBrickedData(self, x, brickSize):
        '''
        Takes x of shape [timeSteps, batchSize, featureDim] and returns bricked
        x of shape [numBricks, brickSize, batchSize, featureDim] by chunking
        along 0-th axes.
        '''
        timeSteps = list(x.size())[0]
        numSplits = int(timeSteps / brickSize)
        batchSize = list(x.size())[1]
        featureDim = list(x.size())[2]
        numBricks = int(timeSteps/brickSize)
        eqlen = numSplits * brickSize
        x = x[:eqlen]
        x_bricked = torch.split(x, numSplits, dim =0)
        x_bricked_batched = torch.cat(x_bricked)
        x_bricked_batched = torch.reshape(x_bricked_batched, (numBricks,brickSize,batchSize,featureDim))
        return x_bricked_batched

    def forward(self, x, brickSize):
        '''
        x: Input data in numpy. Expected to be a 3D tensor  with shape
            [timeStep, batchSize, featureDim]. Note that this is different from
            the convention followed in the TF codebase.
        brickSize: The brick size for the lower dimension. The input data will
            be divided into bricks along the timeStep axis (axis=0) internally
            and fed into the lowest layer RNN. Note that if the last brick has
            fewer than 'brickSize' steps, it will be ignored (no internal
            padding is done).
        '''
        assert x.ndimension() == 3
        assert list(x.size())[2] == self.inputDim
        x_bricks = self.getBrickedData(x, brickSize)
        # This conversion between shapes is tricky. Might infact even be buggy
        # if numpy operations are non-invertible. I've tested to a point but
        # you never know.
        # x bricks: [numBricks, brickSize, batchSize, featureDim]
        x_bricks = x_bricks.permute(1,0,2,3)
        # x bricks: [brickSize, numBricks, batchSize, featureDim]
        oldShape = list(x_bricks.size())
        x_bricks = torch.reshape(x_bricks, [oldShape[0], oldShape[1] * oldShape[2], oldShape[3]])
        # x bricks: [brickSize, numBricks * batchSize, featureDim]
        # x_bricks = torch.Tensor(x_bricks)
        hidd0, out0 = self.rnn0(x_bricks)
        hidd0 = torch.squeeze(hidd0[-1])
        # [numBricks * batchSize, hiddenDim0]
        inp1 = hidd0.view(oldShape[1], oldShape[2], self.hiddenDim0)
        # [numBricks, batchSize, hiddenDim0]
        hidd1, out1 = self.rnn1(inp1)
        hidd1 = torch.squeeze(hidd1[-1])
        out = torch.matmul(hidd1, self.W) + self.B
        return out