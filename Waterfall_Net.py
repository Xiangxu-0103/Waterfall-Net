from os.path import exists, join
from os import makedirs
from sklearn.metrics import confusion_matrix
from helper_tool import DataProcessing as DP
import tensorflow as tf
import numpy as np
import helper_tf_util
import time


def log_out(out_str, f_out):
    f_out.write(out_str + '\n')
    f_out.flush()
    print(out_str)


class Network:
    def __init__(self, dataset, config):
        flat_inputs = dataset.flat_inputs
        self.config = config
        # Path to the result folder
        if self.config.saving:
            if self.config.saving_path is None:
                self.saving_path = time.strftime('results/Log_%Y-%m-%d_%H-%M-%S', time.gmtime())
            else:
                self.saving_path = self.config.saving_path
            makedirs(self.saving_path) if not exists(self.saving_path) else None

        with tf.variable_scope('inputs'):
            self.inputs = dict()
            num_layers = self.config.num_layers
            self.inputs['xyz'] = flat_inputs[:num_layers]
            self.inputs['neigh_idx'] = flat_inputs[num_layers:2 * num_layers]
            self.inputs['sub_idx'] = flat_inputs[2 * num_layers:3 * num_layers]
            self.inputs['interp_idx'] = flat_inputs[3 * num_layers:4 * num_layers]
            self.inputs['sub_xyz'] = flat_inputs[4 * num_layers:5 * num_layers]
            self.inputs['backbone1'] = flat_inputs[5 * num_layers]
            self.inputs['backbone2'] = flat_inputs[5 * num_layers + 1]
            self.inputs['features'] = flat_inputs[5 * num_layers + 2]
            self.inputs['labels'] = flat_inputs[5 * num_layers + 3]
            self.inputs['input_inds'] = flat_inputs[5 * num_layers + 4]
            self.inputs['cloud_inds'] = flat_inputs[5 * num_layers + 5]

            self.labels = self.inputs['labels']
            self.is_training = tf.placeholder(tf.bool, shape=())
            self.training_step = 1
            self.training_epoch = 0
            self.correct_prediction = 0
            self.accuracy = 0
            self.mIoU_list = [0]
            self.class_weights = DP.get_class_weights(dataset.name)
            self.Log_file = open('log_train_' + dataset.name + str(dataset.val_split) + '.txt', 'a')

        with tf.variable_scope('layers'):
            self.logits = self.inference(self.inputs, self.is_training)

        ##################################################################
        # Ignore the invalid point (unlabeled) when calculating the loss #
        ##################################################################
        with tf.variable_scope('loss'):
            self.logits = tf.reshape(self.logits, [-1, config.num_classes])
            self.labels = tf.reshape(self.labels, [-1])

            # Boolean mask of points that should be ignored
            ignored_bool = tf.zeros_like(self.labels, dtype=tf.bool)
            for ign_label in self.config.ignored_label_inds:
                ignored_bool = tf.logical_or(ignored_bool, tf.equal(self.labels, ign_label))

            # Collect logits and labels that are not ignored
            valid_idx = tf.squeeze(tf.where(tf.logical_not(ignored_bool)))
            valid_logits = tf.gather(self.logits, valid_idx, axis=0)
            valid_labels_init = tf.gather(self.labels, valid_idx, axis=0)

            # Reduce label values in the range of logit shape
            reducing_list = tf.range(self.config.num_classes, dtype=tf.int32)
            inserted_value = tf.zeros((1,), dtype=tf.int32)
            for ign_label in self.config.ignored_label_inds:
                reducing_list = tf.concat([reducing_list[:ign_label], inserted_value, reducing_list[ign_label:]], 0)
            valid_labels = tf.gather(reducing_list, valid_labels_init)

            self.loss = self.get_loss(valid_logits, valid_labels, self.class_weights)

        with tf.variable_scope('optimizer'):
            self.learning_rate = tf.Variable(config.learning_rate, trainable=False, name='learning_rate')
            self.train_op = tf.train.AdamOptimizer(self.learning_rate).minimize(self.loss)
            self.extra_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

        with tf.variable_scope('results'):
            self.correct_prediction = tf.nn.in_top_k(valid_logits, valid_labels, 1)
            self.accuracy = tf.reduce_mean(tf.cast(self.correct_prediction, tf.float32))
            self.prob_logits = tf.nn.softmax(self.logits)

            tf.summary.scalar('learning_rate', self.learning_rate)
            tf.summary.scalar('loss', self.loss)
            tf.summary.scalar('accuracy', self.accuracy)

        my_vars = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
        self.saver = tf.train.Saver(my_vars, max_to_keep=100)
        c_proto = tf.ConfigProto()
        c_proto.gpu_options.allow_growth = True
        self.sess = tf.Session(config=c_proto)
        self.merged = tf.summary.merge_all()
        self.train_writer = tf.summary.FileWriter(config.train_sum_dir, self.sess.graph)
        self.sess.run(tf.global_variables_initializer())
        # parameter
        variables = tf.trainable_variables()
        total_parameters = 0
        for variable in variables:
            shape = variable.get_shape()
            variable_parameters = 1
            for dim in shape:
                variable_parameters *= dim.value
            total_parameters += variable_parameters
        print(total_parameters)

    def inference(self, inputs, is_training):

        feature = inputs['features']
        feature = tf.layers.dense(feature, 8, activation=None, name='fc0')
        feature = tf.nn.leaky_relu(tf.layers.batch_normalization(feature, -1, 0.99, 1e-6, training=is_training))
        feature = tf.expand_dims(feature, axis=2)
        f_xyz = []
        for i in range(5):
            f_xyz_i = self.relative_pos_encoding(inputs['xyz'][i], inputs['neigh_idx'][i])
            f_xyz.append(f_xyz_i)

        # backbone 1
        backbone1_conv1, backbone1_down1 = self.conv_downsample(feature, f_xyz[0], inputs['neigh_idx'][0],
                                                                inputs['sub_idx'][0], 16, 'backbone1_1', is_training)
        backbone1_conv2, backbone1_down2 = self.conv_downsample(backbone1_down1, f_xyz[1], inputs['neigh_idx'][1],
                                                                inputs['sub_idx'][1], 32, 'backbone1_2', is_training)
        backbone1_conv3, backbone1_down3 = self.conv_downsample(backbone1_down2, f_xyz[2], inputs['neigh_idx'][2],
                                                                inputs['sub_idx'][2], 64, 'backbone1_3', is_training)
        backbone1_feature = helper_tf_util.conv2d(backbone1_down3, 128, [1, 1], 'backbone1',
                                                  [1, 1], 'VALID', True, is_training)

        # backbone 2
        backbone1_up = self.learn_to_up(inputs['xyz'][1], inputs['xyz'][3], backbone1_feature,
                                        inputs['backbone1'], 'backbone1_up', is_training)
        feature_fuse = helper_tf_util.conv2d_transpose(tf.concat([backbone1_conv2, backbone1_up], axis=-1), 32, [1, 1],
                                                       'fuse2_1', [1, 1], 'VALID', bn=True, is_training=is_training)
        backbone2_conv1, backbone2_down1 = self.conv_downsample(feature_fuse, f_xyz[1], inputs['neigh_idx'][1],
                                                                inputs['sub_idx'][1], 16, 'backbone_2_1', is_training)
        feature_fuse = helper_tf_util.conv2d(tf.concat([backbone2_down1, backbone1_conv3], axis=-1), 32, [1, 1],
                                             'fuse2_2', [1, 1], 'VALID', True, is_training)
        backbone2_conv2, backbone2_down2 = self.conv_downsample(feature_fuse, f_xyz[2], inputs['neigh_idx'][2],
                                                                inputs['sub_idx'][2], 32, 'backbone_2_2', is_training)
        feature_fuse = helper_tf_util.conv2d(tf.concat([backbone2_down2, backbone1_feature], axis=-1), 64, [1, 1],
                                             'fuse2_3', [1, 1], 'VALID', True, is_training)
        backbone2_conv3, backbone2_down3 = self.conv_downsample(feature_fuse, f_xyz[3], inputs['neigh_idx'][3],
                                                                inputs['sub_idx'][3], 64, 'backbone_2_3', is_training)
        backbone2_feature = helper_tf_util.conv2d(backbone2_down3, 128, [1, 1], 'backbone2',
                                                  [1, 1], 'VALID', True, is_training)

        # backbone 3
        backbone2_up = self.learn_to_up(inputs['xyz'][2], inputs['xyz'][4], backbone2_feature,
                                        inputs['backbone2'], 'backbone2_up', is_training)
        feature_fuse = helper_tf_util.conv2d_transpose(tf.concat([backbone2_conv2, backbone2_up], axis=-1), 32, [1, 1],
                                                       'fuse3_1', [1, 1], 'VALID', bn=True, is_training=is_training)
        backbone3_conv1, backbone3_down1 = self.conv_downsample(feature_fuse, f_xyz[2], inputs['neigh_idx'][2],
                                                                inputs['sub_idx'][2], 16, 'backbone_3_1', is_training)
        feature_fuse = helper_tf_util.conv2d(tf.concat([backbone3_down1, backbone2_conv3], axis=-1), 32, [1, 1],
                                             'fuse3_2', [1, 1], 'VALID', True, is_training)
        backbone3_conv2, backbone3_down2 = self.conv_downsample(feature_fuse, f_xyz[3], inputs['neigh_idx'][3],
                                                                inputs['sub_idx'][3], 32, 'backbone_3_2', is_training)
        feature_fuse = helper_tf_util.conv2d(tf.concat([backbone3_down2, backbone2_feature], axis=-1), 64, [1, 1],
                                             'fuse3_3', [1, 1], 'VALID', True, is_training)
        backbone3_conv3, backbone3_down3 = self.conv_downsample(feature_fuse, f_xyz[4], inputs['neigh_idx'][4],
                                                                inputs['sub_idx'][4], 64, 'backbone_3_3', is_training)
        backbone3_feature = helper_tf_util.conv2d(backbone3_down3, 128, [1, 1], 'backbone3',
                                                  [1, 1], 'VALID', True, is_training)

        # Decoder
        f_interp_0 = self.learn_to_up(inputs['xyz'][4], inputs['sub_xyz'][4], backbone3_feature,
                                      inputs['interp_idx'][4], 'learn_to_up_0', is_training)
        f_decoder_0 = helper_tf_util.conv2d_transpose(tf.concat([backbone2_feature, f_interp_0], axis=-1), 128, [1, 1],
                                                      'decoder_0', [1, 1], 'VALID', bn=True, is_training=is_training)

        f_interp_1 = self.learn_to_up(inputs['xyz'][3], inputs['sub_xyz'][3], f_decoder_0,
                                      inputs['interp_idx'][3], 'learn_to_up_1', is_training)
        f_decoder_1 = helper_tf_util.conv2d_transpose(tf.concat([backbone1_feature, f_interp_1], axis=-1), 128, [1, 1],
                                                      'decoder_1', [1, 1], 'VALID', bn=True, is_training=is_training)

        f_interp_2 = self.learn_to_up(inputs['xyz'][2], inputs['sub_xyz'][2], f_decoder_1,
                                      inputs['interp_idx'][2], 'learn_to_up_2', is_training)
        f_decoder_2 = helper_tf_util.conv2d_transpose(tf.concat([backbone3_conv1, f_interp_2], axis=-1), 64, [1, 1],
                                                      'decoder_2', [1, 1], 'VALID', bn=True, is_training=is_training)

        f_interp_3 = self.learn_to_up(inputs['xyz'][1], inputs['sub_xyz'][1], f_decoder_2,
                                      inputs['interp_idx'][1], 'learn_to_up_3', is_training)
        f_decoder_3 = helper_tf_util.conv2d_transpose(tf.concat([backbone2_conv1, f_interp_3], axis=-1), 32, [1, 1],
                                                      'decoder_3', [1, 1], 'VALID', bn=True, is_training=is_training)

        f_interp_4 = self.learn_to_up(inputs['xyz'][0], inputs['sub_xyz'][0], f_decoder_3,
                                      inputs['interp_idx'][0], 'learn_to_up_4', is_training)
        f_decoder_4 = helper_tf_util.conv2d_transpose(tf.concat([backbone1_conv1, f_interp_4], axis=-1), 32, [1, 1],
                                                      'decoder_4', [1, 1], 'VALID', bn=True, is_training=is_training)

        # head
        f_layer_fc1 = helper_tf_util.conv2d(f_decoder_4, 32, [1, 1], 'fc1', [1, 1], 'VALID', True, is_training)
        f_layer_fc2 = helper_tf_util.conv2d(f_layer_fc1, 32, [1, 1], 'fc2', [1, 1], 'VALID', True, is_training)
        f_layer_drop = helper_tf_util.dropout(f_layer_fc2, keep_prob=0.5, is_training=is_training, scope='dp1')
        f_layer_fc3 = helper_tf_util.conv2d(f_layer_drop, self.config.num_classes, [1, 1], 'fc', [1, 1], 'VALID', False,
                                            is_training, activation_fn=None)
        f_out = tf.squeeze(f_layer_fc3, [2])
        return f_out

    def train(self, dataset):
        log_out('****EPOCH {}****'.format(self.training_epoch), self.Log_file)
        self.sess.run(dataset.train_init_op)
        while self.training_epoch < self.config.max_epoch:
            t_start = time.time()
            try:
                ops = [self.train_op,
                       self.extra_update_ops,
                       self.merged,
                       self.loss,
                       self.logits,
                       self.labels,
                       self.accuracy]
                _, _, summary, l_out, probs, labels, acc = self.sess.run(ops, {self.is_training: True})
                self.train_writer.add_summary(summary, self.training_step)
                t_end = time.time()
                if self.training_step % 50 == 0:
                    message = 'Step {:08d} L_out={:5.3f} Acc={:4.2f} ''---{:8.2f} ms/batch'
                    log_out(message.format(self.training_step, l_out, acc, 1000 * (t_end - t_start)), self.Log_file)
                self.training_step += 1

            except tf.errors.OutOfRangeError:

                m_iou = self.evaluate(dataset)
                if m_iou > np.max(self.mIoU_list):
                    # Save the best model
                    snapshot_directory = join(self.saving_path, 'snapshots')
                    makedirs(snapshot_directory) if not exists(snapshot_directory) else None
                    self.saver.save(self.sess, snapshot_directory + '/snap', global_step=self.training_step)
                self.mIoU_list.append(m_iou)
                log_out('Best m_IoU is: {:5.3f}'.format(max(self.mIoU_list)), self.Log_file)

                self.training_epoch += 1
                self.sess.run(dataset.train_init_op)
                # Update learning rate
                op = self.learning_rate.assign(tf.multiply(self.learning_rate,
                                                           self.config.lr_decays[self.training_epoch]))
                self.sess.run(op)
                log_out('****EPOCH {}****'.format(self.training_epoch), self.Log_file)

            except tf.errors.InvalidArgumentError as e:

                print('Caught a NaN error :')
                print(e.error_code)
                print(e.message)
                print(e.op)
                print(e.op.name)
                print([t.name for t in e.op.inputs])
                print([t.name for t in e.op.outputs])

                a = 1 / 0

        print('finished')
        self.sess.close()

    def evaluate(self, dataset):

        # Initialise iterator with validation data
        self.sess.run(dataset.val_init_op)

        gt_classes = [0 for _ in range(self.config.num_classes)]
        positive_classes = [0 for _ in range(self.config.num_classes)]
        true_positive_classes = [0 for _ in range(self.config.num_classes)]
        val_total_correct = 0
        val_total_seen = 0

        for step_id in range(self.config.val_steps):
            if step_id % 50 == 0:
                print(str(step_id) + '/' + str(self.config.val_steps))
            try:
                ops = (self.prob_logits, self.labels, self.accuracy)
                stacked_prob, labels, acc = self.sess.run(ops, {self.is_training: False})
                pred = np.argmax(stacked_prob, 1)
                if not self.config.ignored_label_inds:
                    pred_valid = pred
                    labels_valid = labels
                else:
                    invalid_idx = np.where(labels == self.config.ignored_label_inds)[0]
                    labels_valid = np.delete(labels, invalid_idx)
                    labels_valid = labels_valid - 1
                    pred_valid = np.delete(pred, invalid_idx)

                correct = np.sum(pred_valid == labels_valid)
                val_total_correct += correct
                val_total_seen += len(labels_valid)

                conf_matrix = confusion_matrix(labels_valid, pred_valid, np.arange(0, self.config.num_classes, 1))
                gt_classes += np.sum(conf_matrix, axis=1)
                positive_classes += np.sum(conf_matrix, axis=0)
                true_positive_classes += np.diagonal(conf_matrix)

            except tf.errors.OutOfRangeError:
                break

        iou_list = []
        for n in range(0, self.config.num_classes, 1):
            iou = true_positive_classes[n] / float(gt_classes[n] + positive_classes[n] - true_positive_classes[n])
            iou_list.append(iou)
        mean_iou = sum(iou_list) / float(self.config.num_classes)

        log_out('eval accuracy: {}'.format(val_total_correct / float(val_total_seen)), self.Log_file)
        log_out('mean IoU: {}'.format(mean_iou), self.Log_file)

        mean_iou = 100 * mean_iou
        log_out('Mean IoU = {:.1f}%'.format(mean_iou), self.Log_file)
        s = '{:5.2f} | '.format(mean_iou)
        for IoU in iou_list:
            s += '{:5.2f} '.format(100 * IoU)
        log_out('-' * len(s), self.Log_file)
        log_out(s, self.Log_file)
        log_out('-' * len(s) + '\n', self.Log_file)
        return mean_iou

    def get_loss(self, logits, labels, pre_cal_weights):
        # calculate the weighted cross entropy according to the inverse frequency
        class_weights = tf.convert_to_tensor(pre_cal_weights, dtype=tf.float32)
        one_hot_labels = tf.one_hot(labels, depth=self.config.num_classes)
        weights = tf.reduce_sum(class_weights * one_hot_labels, axis=1)
        unweighted_losses = tf.nn.softmax_cross_entropy_with_logits(logits=logits, labels=one_hot_labels)
        weighted_losses = unweighted_losses * weights
        output_loss = tf.reduce_mean(weighted_losses)
        return output_loss

    def conv_downsample(self, feature, f_xyz, neigh_idx, sub_idx, d_out, name, is_training):
        f_conv = self.dilated_res_block(feature, f_xyz, neigh_idx, d_out, name + 'conv', is_training)
        f_down = self.random_sample(f_conv, sub_idx)
        return f_conv, f_down

    def dilated_res_block(self, feature, f_xyz, neigh_idx, d_out, name, is_training):
        f_pc = helper_tf_util.conv2d(feature, d_out // 2, [1, 1], name + 'mlp1', [1, 1], 'VALID', True, is_training)
        f_pc = self.building_block(f_xyz, f_pc, neigh_idx, d_out, name + 'LFA', is_training)
        f_pc = helper_tf_util.conv2d(f_pc, d_out * 2, [1, 1], name + 'mlp2', [1, 1], 'VALID', True, is_training,
                                     activation_fn=False)
        shortcut = helper_tf_util.conv2d(feature, d_out * 2, [1, 1], name + 'shortcut', [1, 1], 'VALID',
                                         activation_fn=False, bn=True, is_training=is_training)
        return tf.nn.leaky_relu(f_pc + shortcut)

    def building_block(self, f_xyz, feature, neigh_idx, d_out, name, is_training):
        d_in = feature.get_shape()[-1].value
        f_xyz = helper_tf_util.conv2d(f_xyz, d_in, [1, 1], name + 'mlp1', [1, 1], 'VALID', True, is_training)
        f_neighbours = self.gather_neighbour(tf.squeeze(feature, axis=2), neigh_idx)
        f_concat = tf.concat([f_neighbours, f_xyz], axis=-1)
        f_pc_agg = self.att_pooling(f_concat, d_out // 2, name + 'att_pooling_1', is_training)

        f_xyz = helper_tf_util.conv2d(f_xyz, d_out // 2, [1, 1], name + 'mlp2', [1, 1], 'VALID', True, is_training)
        f_neighbours = self.gather_neighbour(tf.squeeze(f_pc_agg, axis=2), neigh_idx)
        f_concat = tf.concat([f_neighbours, f_xyz], axis=-1)
        f_pc_agg = self.att_pooling(f_concat, d_out, name + 'att_pooling_2', is_training)
        return f_pc_agg

    def relative_pos_encoding(self, xyz, neigh_idx):
        neighbor_xyz = self.gather_neighbour(xyz, neigh_idx)
        xyz_tile = tf.tile(tf.expand_dims(xyz, axis=2), [1, 1, tf.shape(neigh_idx)[-1], 1])
        relative_xyz = xyz_tile - neighbor_xyz
        relative_dis = tf.sqrt(tf.reduce_sum(tf.square(relative_xyz), axis=-1, keepdims=True))
        relative_feature = tf.concat([relative_dis, relative_xyz, xyz_tile, neighbor_xyz], axis=-1)
        return relative_feature

    def learn_to_up(self, xyz, sub_xyz, feature, interp_idx, name, is_training):
        neighbor_xyz = self.nearest_interpolation(sub_xyz, interp_idx)
        xyz_tile = tf.tile(tf.expand_dims(xyz, axis=2), [1, 1, tf.shape(interp_idx)[-1], 1])
        relative_xyz = neighbor_xyz - xyz_tile

        neighbor_features = self.nearest_interpolation(tf.squeeze(feature, axis=2), interp_idx)
        f_xyz = helper_tf_util.conv2d(relative_xyz, 8, [1, 1], name + 'mlp1', [1, 1], 'VALID', True, is_training)
        f_xyz = helper_tf_util.conv2d(f_xyz, 1, [1, 1], name + 'mlp2', [1, 1], 'VALID',
                                      bn=False, is_training=is_training, activation_fn=False)
        f_xyz = tf.nn.softmax(f_xyz, axis=2)
        f_pc = f_xyz * neighbor_features
        f_pc = tf.reduce_sum(f_pc, axis=2, keepdims=True)
        return f_pc

    @staticmethod
    def random_sample(feature, pool_idx):
        """
        :param feature: [B, N, d] input features matrix
        :param pool_idx: [B, N', max_num] N' < N, N' is the selected position after pooling
        :return: pool_features = [B, N', d] pooled features matrix
        """
        feature = tf.squeeze(feature, axis=2)
        num_neigh = tf.shape(pool_idx)[-1]
        d = feature.get_shape()[-1]
        batch_size = tf.shape(pool_idx)[0]
        pool_idx = tf.reshape(pool_idx, [batch_size, -1])
        pool_features = tf.batch_gather(feature, pool_idx)
        pool_features = tf.reshape(pool_features, [batch_size, -1, num_neigh, d])
        pool_features = tf.reduce_max(pool_features, axis=2, keepdims=True)
        return pool_features

    @staticmethod
    def nearest_interpolation(feature, interp_idx):
        """
        :param feature: [B, N, d] input features matrix
        :param interp_idx: [B, up_num_points, 1] nearest neighbour index
        :return: [B, up_num_points, d] interpolated features matrix
        """
        batch_size = tf.shape(interp_idx)[0]
        up_num_points = tf.shape(interp_idx)[1]
        d = feature.get_shape()[2].value
        interp_idx = tf.reshape(interp_idx, [batch_size, -1])
        interpolated_features = tf.batch_gather(feature, interp_idx)
        interpolated_features = tf.reshape(interpolated_features, [batch_size, up_num_points, -1, d])
        return interpolated_features

    @staticmethod
    def gather_neighbour(pc, neighbor_idx):
        # gather the coordinates or features of neighboring points
        batch_size = tf.shape(pc)[0]
        num_points = tf.shape(pc)[1]
        d = pc.get_shape()[2].value
        index_input = tf.reshape(neighbor_idx, shape=[batch_size, -1])
        features = tf.batch_gather(pc, index_input)
        features = tf.reshape(features, [batch_size, num_points, tf.shape(neighbor_idx)[-1], d])
        return features

    @staticmethod
    def att_pooling(feature_set, d_out, name, is_training):
        batch_size = tf.shape(feature_set)[0]
        num_points = tf.shape(feature_set)[1]
        num_neigh = tf.shape(feature_set)[2]
        d = feature_set.get_shape()[3].value
        f_reshaped = tf.reshape(feature_set, shape=[-1, num_neigh, d])
        att_activation = tf.layers.dense(f_reshaped, d, activation=None, use_bias=False, name=name + 'fc')
        att_scores = tf.nn.softmax(att_activation, axis=1)
        f_agg = f_reshaped * att_scores
        f_agg = tf.reduce_sum(f_agg, axis=1)
        f_agg = tf.reshape(f_agg, [batch_size, num_points, 1, d])
        f_agg = helper_tf_util.conv2d(f_agg, d_out, [1, 1], name + 'mlp', [1, 1], 'VALID', True, is_training)
        return f_agg
