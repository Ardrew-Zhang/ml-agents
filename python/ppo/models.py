import numpy as np
import tensorflow as tf
import tensorflow.contrib.layers as c_layers
from tensorflow.python.tools import freeze_graph
from unityagents import UnityEnvironmentException


def create_agent_model(env, lr=1e-4, h_size=128, epsilon=0.2, beta=1e-3, max_step=5e6):
    """
    Takes a Unity environment and model-specific hyper-parameters and returns the
    appropriate PPO agent model for the environment.
    :param env: a Unity environment.
    :param lr: Learning rate.
    :param h_size: Size of hidden layers/
    :param epsilon: Value for policy-divergence threshold.
    :param beta: Strength of entropy regularization.
    :return: a sub-class of PPOAgent tailored to the environment.
    :param max_step: Total number of training steps.
    """
    brain_name = env.brain_names[0]
    brain = env.brains[brain_name]
    if brain.action_space_type == "continuous":
        if brain.number_observations == 0:
            return ContinuousControlModel(lr, brain, h_size, epsilon, max_step)
        else:
            raise UnityEnvironmentException("There is currently no PPO model which supports both a continuous "
                                            "action space and camera observations.")
    if brain.action_space_type == "discrete":
        return DiscreteControlModel(lr, brain, h_size, epsilon, beta, max_step)


def save_model(sess, saver, model_path="./", steps=0):
    """
    Saves current model to checkpoint folder.
    :param sess: Current Tensorflow session.
    :param model_path: Designated model path.
    :param steps: Current number of steps in training process.
    :param saver: Tensorflow saver for session.
    """
    last_checkpoint = model_path + '/model-' + str(steps) + '.cptk'
    saver.save(sess, last_checkpoint)
    tf.train.write_graph(sess.graph_def, model_path, 'raw_graph_def.pb', as_text=False)
    print("Saved Model")


def export_graph(model_path, env_name="env", target_nodes="action"):
    """
    Exports latest saved model to .bytes format for Unity embedding.
    :param model_path: path of model checkpoints.
    :param env_name: Name of associated Learning Environment.
    :param target_nodes: Comma separated string of needed output nodes for embedded graph.
    """
    ckpt = tf.train.get_checkpoint_state(model_path)
    freeze_graph.freeze_graph(input_graph=model_path + '/raw_graph_def.pb',
                              input_binary=True,
                              input_checkpoint=ckpt.model_checkpoint_path,
                              output_node_names=target_nodes,
                              output_graph=model_path + '/' + env_name + '.bytes',
                              clear_devices=True, initializer_nodes="", input_saver="",
                              restore_op_name="save/restore_all", filename_tensor_name="save/Const:0")


class PPOModel(object):
    def create_visual_encoder(self, o_size_h, o_size_w, h_size):
        self.observation_in = tf.placeholder(shape=[None, o_size_h, o_size_w, 1], dtype=tf.float32,
                                             name='observation_0')
        self.conv1 = tf.layers.conv2d(self.observation_in, 32, kernel_size=[3, 3], strides=[2, 2],
                                      use_bias=False, activation=tf.nn.elu)
        self.conv2 = tf.layers.conv2d(self.conv1, 64, kernel_size=[3, 3], strides=[2, 2],
                                      use_bias=False, activation=tf.nn.elu)
        hidden = tf.layers.dense(c_layers.flatten(self.conv2), h_size, use_bias=False, activation=tf.nn.elu)
        return hidden

    def create_continuous_state_encoder(self, s_size, h_size):
        self.state_in = tf.placeholder(shape=[None, s_size], dtype=tf.float32, name='state')
        hidden_1 = tf.layers.dense(self.state_in, h_size, use_bias=False, activation=tf.nn.elu)
        hidden_2 = tf.layers.dense(hidden_1, h_size, use_bias=False, activation=tf.nn.elu)
        return hidden_2

    def create_discrete_state_encoder(self, s_size, h_size):
        self.state_in = tf.placeholder(shape=[None, 1], dtype=tf.int32, name='state')
        state_in = tf.reshape(self.state_in, [-1])
        state_onehot = c_layers.one_hot_encoding(state_in, s_size)
        hidden = tf.layers.dense(state_onehot, h_size, activation=tf.nn.elu)
        return hidden

    def create_ppo_optimizer(self, probs, old_probs, value, entropy, beta, epsilon, lr, max_step):
        """
        Creates training-specific Tensorflow ops for PPO models.
        :param probs: Current policy probabilities
        :param old_probs: Past policy probabilities
        :param value: Current value estimate
        :param beta: Entropy regularization strength
        :param entropy: Current policy entropy
        :param epsilon: Value for policy-divergence threshold
        :param lr: Learning rate
        :param max_step: Total number of training steps.
        """
        self.returns_holder = tf.placeholder(shape=[None], dtype=tf.float32, name='discounted_rewards')
        self.advantage = tf.placeholder(shape=[None, 1], dtype=tf.float32, name='advantages')

        r_theta = probs / old_probs
        p_opt_a = r_theta * self.advantage
        p_opt_b = tf.clip_by_value(r_theta, 1 - epsilon, 1 + epsilon) * self.advantage
        self.policy_loss = -tf.reduce_mean(tf.minimum(p_opt_a, p_opt_b))

        self.value_loss = tf.reduce_mean(tf.squared_difference(self.returns_holder,
                                                               tf.reduce_sum(value, axis=1)))

        self.loss = self.policy_loss + self.value_loss - beta * tf.reduce_mean(entropy)

        self.global_step = tf.Variable(0, trainable=False, name='global_step', dtype=tf.int32)
        self.learning_rate = tf.train.polynomial_decay(lr, self.global_step,
                                                       max_step, 1e-10,
                                                       power=1.0)
        optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        self.update_batch = optimizer.minimize(self.loss)

        self.increment_step = tf.assign(self.global_step, self.global_step + 1)


class ContinuousControlModel(PPOModel):
    def __init__(self, lr, brain, h_size, epsilon, max_step):
        """
        Creates Continuous Control Actor-Critic model.
        :param s_size: State-space size
        :param a_size: Action-space size
        :param h_size: Hidden layer size
        """
        s_size = brain.state_space_size
        a_size = brain.action_space_size

        self.state_in = tf.placeholder(shape=[None, s_size], dtype=tf.float32, name='state')
        self.batch_size = tf.placeholder(shape=None, dtype=tf.int32, name='batch_size')
        hidden_policy = tf.layers.dense(self.state_in, h_size, use_bias=False, activation=tf.nn.tanh)
        hidden_value = tf.layers.dense(self.state_in, h_size, use_bias=False, activation=tf.nn.tanh)
        hidden_policy_2 = tf.layers.dense(hidden_policy, h_size, use_bias=False, activation=tf.nn.tanh)
        hidden_value_2 = tf.layers.dense(hidden_value, h_size, use_bias=False, activation=tf.nn.tanh)

        self.mu = tf.layers.dense(hidden_policy_2, a_size, activation=None, use_bias=False,
                                  kernel_initializer=c_layers.variance_scaling_initializer(factor=0.1))
        self.log_sigma_sq = tf.Variable(tf.zeros([a_size]))
        self.sigma_sq = tf.exp(self.log_sigma_sq)

        self.epsilon = tf.placeholder(shape=[None, a_size], dtype=tf.float32, name='epsilon')

        self.output = self.mu + tf.sqrt(self.sigma_sq) * self.epsilon
        self.output = tf.identity(self.output, name='action')

        a = tf.exp(-1 * tf.pow(tf.stop_gradient(self.output) - self.mu, 2) / (2 * self.sigma_sq))
        b = 1 / tf.sqrt(2 * self.sigma_sq * np.pi)
        self.probs = a * b

        self.entropy = tf.reduce_sum(0.5 * tf.log(2 * np.pi * np.e * self.sigma_sq))

        self.value = tf.layers.dense(hidden_value_2, 1, activation=None, use_bias=False)

        self.old_probs = tf.placeholder(shape=[None, a_size], dtype=tf.float32, name='old_probabilities')

        self.create_ppo_optimizer(self.probs, self.old_probs, self.value, self.entropy, 0.0, epsilon, lr, max_step)


class DiscreteControlModel(PPOModel):
    def __init__(self, lr, brain, h_size, epsilon, beta, max_step):
        """
        Creates Discrete Control Actor-Critic model.
        :param brain: State-space size
        :param h_size: Hidden layer size
        """
        hidden_state, hidden_visual, hidden = None, None, None
        if brain.number_observations > 0:
            h_size, w_size = brain.camera_resolutions[0]['height'], brain.camera_resolutions[0]['height']
            hidden_visual = self.create_visual_encoder(h_size, w_size, h_size)
        if brain.state_space_size > 0:
            s_size = brain.state_space_size
            if brain.state_space_type == "continuous":
                hidden_state = self.create_continuous_state_encoder(s_size, h_size)
            else:
                hidden_state = self.create_discrete_state_encoder(s_size, h_size)

        if hidden_visual is None and hidden_state is None:
            raise Exception("No valid network configuration possible. "
                            "There are no states or observations in this brain")
        elif hidden_visual is not None and hidden_state is None:
            hidden = hidden_visual
        elif hidden_visual is None and hidden_state is not None:
            hidden = hidden_state
        elif hidden_visual is not None and hidden_state is not None:
            hidden = tf.concat([hidden_visual, hidden_state], axis=1)

        a_size = brain.action_space_size

        self.batch_size = tf.placeholder(shape=None, dtype=tf.int32, name='batch_size')
        self.policy = tf.layers.dense(hidden, a_size, activation=None, use_bias=False,
                                      kernel_initializer=c_layers.variance_scaling_initializer(factor=0.1))
        self.probs = tf.nn.softmax(self.policy)
        self.action = tf.multinomial(self.policy, 1)
        self.output = tf.identity(self.action, name='action')
        self.value = tf.layers.dense(hidden, 1, activation=None, use_bias=False)

        self.entropy = -tf.reduce_sum(self.probs * tf.log(self.probs + 1e-10), axis=1)

        self.action_holder = tf.placeholder(shape=[None], dtype=tf.int32)
        self.selected_actions = c_layers.one_hot_encoding(self.action_holder, a_size)
        self.old_probs = tf.placeholder(shape=[None, a_size], dtype=tf.float32, name='old_probabilities')
        self.responsible_probs = tf.reduce_sum(self.probs * self.selected_actions, axis=1)
        self.old_responsible_probs = tf.reduce_sum(self.old_probs * self.selected_actions, axis=1)

        self.create_ppo_optimizer(self.responsible_probs, self.old_responsible_probs,
                                  self.value, self.entropy, beta, epsilon, lr, max_step)