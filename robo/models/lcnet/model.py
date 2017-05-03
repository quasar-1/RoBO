import logging
import lasagne
import numpy as np

import theano
import theano.tensor as T

from copy import deepcopy

from robo.models.bnn import BayesianNeuralNetwork


from lc_prediction.lc_layers import BasisFunctionLayer


def get_lc_net(n_inputs):
    l_in = lasagne.layers.InputLayer(shape=(None, n_inputs))

    only_x = lasagne.layers.SliceLayer(l_in, slice(0, n_inputs - 1), axis=1)
    only_t = lasagne.layers.SliceLayer(l_in, slice(-1, None), axis=1)

    fc_layer_1 = lasagne.layers.DenseLayer(
        only_x,
        num_units=64,
        W=lasagne.init.HeNormal(),
        b=lasagne.init.Constant(val=0.0),
        nonlinearity=lasagne.nonlinearities.tanh)

    fc_layer_2 = lasagne.layers.DenseLayer(
        fc_layer_1,
        num_units=64,
        W=lasagne.init.HeNormal(),
        b=lasagne.init.Constant(val=0.0),
        nonlinearity=lasagne.nonlinearities.tanh)

    fc_layer_3 = lasagne.layers.DenseLayer(
        fc_layer_2,
        num_units=64,
        W=lasagne.init.HeNormal(),
        b=lasagne.init.Constant(val=0.0),
        nonlinearity=lasagne.nonlinearities.tanh)

    theta_layer = lasagne.layers.DenseLayer(
        fc_layer_3,
        num_units=13,
        W=lasagne.init.HeNormal(),
        b=lasagne.init.Constant(val=0.0),
        nonlinearity=lasagne.nonlinearities.tanh)

    bf_layer = BasisFunctionLayer((only_t, theta_layer))

    weight_layer = lasagne.layers.DenseLayer(fc_layer_3,
                                             num_units=5,
                                             W=lasagne.init.Constant(1e-2),
                                             nonlinearity=lasagne.nonlinearities.softmax)

    l_res = lasagne.layers.ElemwiseMergeLayer([bf_layer, weight_layer], merge_function=T.mul)

    l_res = lasagne.layers.DenseLayer(l_res,
                                      num_units=1,
                                      W=lasagne.init.Constant(1),
                                      b=lasagne.init.Constant(0),
                                      nonlinearity=lasagne.nonlinearities.linear)
    l_res.params[l_res.W].remove("trainable")
    l_res.params[l_res.b].remove("trainable")

    l_mean = lasagne.layers.DenseLayer(fc_layer_3,
                                       num_units=1,
                                       W=lasagne.init.HeNormal(),
                                       nonlinearity=lasagne.nonlinearities.sigmoid)

    l_out = lasagne.layers.ElemwiseSumLayer([l_res, l_mean])

    l_sigma = lasagne.layers.DenseLayer(fc_layer_3,
                                        num_units=1,
                                        W=lasagne.init.HeNormal(),
                                        nonlinearity=lasagne.nonlinearities.sigmoid)

    network = lasagne.layers.ConcatLayer([l_out, l_sigma], axis=1)

    return network


class LCNet(BayesianNeuralNetwork):
    def __init__(self, sampling_method="sghmc",
                 n_nets=100, l_rate=1e-3,
                 mdecay=5e-2, n_iters=5 * 10 ** 4,
                 bsize=20, burn_in=1000,
                 precondition=True, sample_steps=100,
                 rng=None, get_net=get_lc_net):

        super(LCNet, self).__init__(sampling_method=sampling_method,
                                    n_nets=n_nets, l_rate=l_rate,
                                    mdecay=mdecay, n_iters=n_iters,
                                    bsize=bsize, burn_in=burn_in,
                                    precondition=precondition,
                                    rng=rng, get_net=get_net,
                                    sample_steps=sample_steps,
                                    normalize_input=False,
                                    normalize_output=False)

    def train(self, X, y):
        X_, self.x_mean, self.x_std = self.normalize_inputs(X)
        super(LCNet, self).train(X_, y)

    def sample_functions(self, X_test, n_funcs=1):
        X_test_norm, _, _ = self.normalize_inputs(X_test, self.x_mean, self.x_std)
        return super(LCNet, self).sample_functions(X_test, n_funcs)

    def get_incumbent(self):
        inc, inc_value = super(LCNet, self).get_incumbent()
        if self.normalize_input:
            inc = self.unnormalize_inputs(inc, self.x_mean, self.x_std)

        return inc, inc_value

    def negativ_log_likelihood(self, f_net, X, y, n_examples, weight_prior, variance_prior):

        f_out = lasagne.layers.get_output(f_net, X)
        f_mean = f_out[:, 0].reshape((-1, 1))

        # Scale the noise to be between -10 and 10 on a log scale
        f_log_var = 20 * f_out[:, 1].reshape((-1, 1)) - 10

        f_var_inv = 1. / (T.exp(f_log_var) + 1e-16)
        mse = T.square(y - f_mean)
        log_like = T.sum(T.sum(-mse * (0.5 * f_var_inv) - 0.5 * f_log_var, axis=1))
        # scale by batch size to make this work nicely with the updaters above
        log_like /= T.cast(X.shape[0], theano.config.floatX)
        # scale the priors by the dataset size for the same reason
        # prior for the variance
        tn_examples = T.cast(n_examples, theano.config.floatX)
        log_like += variance_prior.log_like(f_log_var) / tn_examples
        # prior for the weights
        params = lasagne.layers.get_all_params(f_net, trainable=True)
        log_like += weight_prior.log_like(params) / tn_examples

        return -log_like, T.mean(mse)

    def predict(self, X_test, return_individual_predictions=False):
        """
        Returns the predictive mean and variance of the objective function at
        the given test points.

        Parameters
        ----------
        X_test: np.ndarray (N, D)
            Input test points

        Returns
        ----------
        np.array(N,)
            predictive mean
        np.array(N,)
            predictive variance

        """

        if not self.is_trained:
            logging.error("Model is not trained!")
            return

        # Normalize input
        X_, _, _ = self.normalize_inputs(X_test, self.x_mean, self.x_std)

        f_out = []
        theta_noise = []
        for sample in self.samples:
            lasagne.layers.set_all_param_values(self.net, sample)
            out = self.single_predict(X_)
            f_out.append(out[:, 0])
            # Log noise is a sigmoid output [0, 1] scale it to be in [-10, 10]
            theta_noise.append(np.exp(20 * out[:, 1] - 10))

        f_out = np.asarray(f_out)
        theta_noise = np.asarray(theta_noise)

        if return_individual_predictions:
            return f_out, theta_noise

        m = np.mean(f_out, axis=0)
        v = np.mean((f_out - m) ** 2, axis=0)

        return m, v

    @staticmethod
    def normalize_inputs(x, mean=None, std=None):
        if mean is None:
            mean = np.mean(x, axis=0)
        if std is None:
            std = np.std(x, axis=0)

        x_norm = deepcopy(x)
        x_norm[:, :-1] = (x[:, :-1] - mean[:-1]) / std[:-1]
        return x_norm, mean, std

    @staticmethod
    def unnormalize_inputs(x, mean, std):
        return x[:, :-1] * std[:, :-1] + mean[:, :-1]
