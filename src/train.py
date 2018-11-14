# CNN-LSTM-CTC-OCR
# Copyright (C) 2017 Jerod Weinman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import tensorflow as tf
from tensorflow.contrib import learn
from config import log
import data_queue
import model
import denseNet


FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string('output','../data/model',
                          """Directory for event logs and checkpoints""")
tf.app.flags.DEFINE_string('tune_from','',
                          """Path to pre-trained model checkpoint""")
tf.app.flags.DEFINE_string('tune_scope','',
                          """Variable scope for training""")

tf.app.flags.DEFINE_integer('batch_size',1,
                            """Mini-batch size""")#32
tf.app.flags.DEFINE_float('learning_rate',(1e-3)/3.74,#1e-4
                          """Initial learning rate""")#0.0001
tf.app.flags.DEFINE_float('momentum',0.9,
                          """Optimizer gradient first-order momentum""")#
tf.app.flags.DEFINE_float('decay_rate',0.9,
                          """Learning rate decay base""")
tf.app.flags.DEFINE_float('decay_steps',2**16,
                          """Learning rate decay exponent scale""")#65536
tf.app.flags.DEFINE_float('decay_staircase',False,
                          """Staircase learning rate decay by integer division""")


tf.app.flags.DEFINE_integer('max_num_steps', 2**26,
                            """Number of optimization steps to run""")#200k

tf.app.flags.DEFINE_string('train_device','/gpu:1',
                           """Device for training graph placement""")
tf.app.flags.DEFINE_string('train_input_device','/cpu:1',
                           """Device for train data preprocess/batching graph placement""")
tf.app.flags.DEFINE_string("val_input_device", "/cpu:2",
                           """Device for validation data preprocess/batching graph placement""")

tf.app.flags.DEFINE_string('train_path','../data/train/',
                           """Base directory for training data""")
tf.app.flags.DEFINE_string("val_path", "../data/val/",
                           """Base directory for validating data""")
tf.app.flags.DEFINE_string('filename_pattern','words-*',
                           """File pattern for input data""")
tf.app.flags.DEFINE_integer('num_input_threads',1,    #4
                          """Number of readers for input data""")
tf.app.flags.DEFINE_integer('width_threshold',None,
                            """Limit of input image width""")
tf.app.flags.DEFINE_integer('length_threshold',None,
                            """Limit of input string length width""")
tf.app.flags.DEFINE_integer("num_epochs", None,
                            """number of epochs for input queue""")
tf.app.flags.DEFINE_float("train_val_split", 0.1,
                          """train validation data split ratio""")
tf.logging.set_verbosity(tf.logging.INFO)

# Non-configurable parameters
optimizer='Adam'
mode = learn.ModeKeys.TRAIN # 'Configure' training mode for dropout layers

def _get_bucketed_input(data_dir,
                        filename_pattern,
                        batch_size,
                        num_threads,
                        input_device,
                        width_threshold,
                        length_threshold,
                        num_epochs,
                        ):
    """Set up and return image, label, and image width tensors"""

    image, width, label, length, _,_=data_queue.bucketed_input_pipeline(
        data_dir,
        str.split(filename_pattern, ','),
        batch_size= batch_size,
        num_threads= num_threads,
        input_device= input_device,
        width_threshold= width_threshold,
        length_threshold= length_threshold,
        num_epochs= num_epochs
    )

    #tf.summary.image('images',image) # Uncomment to see images in TensorBoard
    return image, width, label, length


def _get_threaded_input(
        data_dir,
        filename_pattern,
        batch_size,
        num_threads,
        input_device,
        num_epochs,
):
    """Set up and return image, label, width and text tensors"""

    image, width, label, length, text, filename = data_queue.threaded_input_pipeline(
        data_dir,
        str.split(filename_pattern, ','),
        batch_size= batch_size,
        num_threads= num_threads,
        num_epochs= num_epochs,  # Repeat for streaming
        batch_device= input_device,
        preprocess_device= input_device)

    return image, width, label, length

def _add_loss(logits, label, sequence_length):
    with tf.name_scope("loss"):
        loss = model.ctc_loss_layer(logits, label, sequence_length)
        tf.summary.scalar("loss", loss)
    return loss

def _add_optimizer(loss):
    """Set up training ops"""
    with tf.name_scope("train"):

        if FLAGS.tune_scope:
            scope=FLAGS.tune_scope
        else:
            scope="convnet|rnn"

        rnn_vars = tf.get_collection( tf.GraphKeys.TRAINABLE_VARIABLES,
                                       scope=scope)

        # Update batch norm stats [http://stackoverflow.com/questions/43234667]
        extra_update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

        with tf.control_dependencies(extra_update_ops):

            learning_rate = tf.train.exponential_decay(
                FLAGS.learning_rate,
                tf.train.get_global_step(),
                FLAGS.decay_steps,
                FLAGS.decay_rate,
                staircase=FLAGS.decay_staircase,
                name='learning_rate')

            optimizer = tf.train.AdamOptimizer(
                learning_rate=learning_rate,
                beta1=FLAGS.momentum)

            optimizer_ops = tf.contrib.layers.optimize_loss(
                loss=loss,
                global_step=tf.train.get_global_step(),
                learning_rate=learning_rate,
                optimizer=optimizer,
                variables=rnn_vars)

            tf.summary.scalar( 'learning_rate', learning_rate )

    return optimizer_ops

def _add_metrics(rnn_logits, sequence_length, label, label_length):
    """Create ops for testing (all scalars):
       loss: CTC loss function value,
       label_error:  Batch-normalized edit distance on beam search max
       sequence_error: Batch-normalized sequence error rate
    """
    with tf.name_scope("metrics"):
        predictions,_ = tf.nn.ctc_beam_search_decoder(rnn_logits,
                                                   sequence_length,
                                                   beam_width=128,
                                                   top_paths=1,
                                                   merge_repeated=True)
        hypothesis = tf.cast(predictions[0], tf.int32) # for edit_distance
        label_errors = tf.edit_distance(hypothesis, label, normalize=False)
        sequence_errors = tf.count_nonzero(label_errors,axis=0)
        total_label_error = tf.reduce_sum( label_errors )
        total_labels = tf.reduce_sum( label_length )
        label_error = tf.truediv( total_label_error,
                                  tf.cast(total_labels, tf.float32 ),
                                  name='label_error')
        sequence_error = tf.truediv( tf.cast( sequence_errors, tf.int32 ),
                                     tf.shape(label_length)[0], # batchsize
                                     name='sequence_error')
        tf.summary.scalar( 'label_error', label_error )
        tf.summary.scalar( 'sequence_error', sequence_error )

    return label_error, sequence_error


def _get_session_config():
    """Setup session config to soften device placement"""

    config=tf.ConfigProto(
        allow_soft_placement=True, 
        log_device_placement=False)

    return config

def _get_init_pretrained():
    """Return lambda for reading pretrained initial model"""

    if not FLAGS.tune_from:
        return None
    
    saver_reader = tf.train.Saver(
        tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES))

    ckpt_path=FLAGS.tune_from

    init_fn = lambda sess: saver_reader.restore(sess, ckpt_path)

    return init_fn


def main(argv=None):

    with tf.Graph().as_default():
        global_step = tf.train.get_or_create_global_step()

        # isTraining= tf.placeholder(tf.bool, shape= (), name= "isTraining")
        isTraining= True
        image, width, label, length= _get_bucketed_input(
            data_dir= FLAGS.train_path,
            filename_pattern= FLAGS.filename_pattern,
            batch_size= FLAGS.batch_size,
            num_threads= FLAGS.num_input_threads,
            input_device= FLAGS.train_input_device,
            width_threshold= FLAGS.width_threshold,
            length_threshold= FLAGS.length_threshold,
            num_epochs= FLAGS.num_epochs,
        )
        # valImg, valWidth, vallabel, valLength= _get_threaded_input(
        #         data_dir=FLAGS.val_path,
        #         filename_pattern=FLAGS.filename_pattern,
        #         batch_size=FLAGS.batch_size,
        #         num_threads=FLAGS.num_input_threads,
        #     input_device=FLAGS.val_input_device,
        #     num_epochs= FLAGS.num_epochs,
        # )

        # image, width,label, length= tf.cond(
        #     isTraining,
        #     true_fn= lambda: (trainImg, trainWidth, trainLabel, trainLength),
        #     false_fn= lambda : (valImg, valWidth, vallabel, valLength),
        # )


        with tf.device(FLAGS.train_device):

            # features,sequence_length = model.convnet_layers( image, width, isTraining) # mode: training mode for dropout layer, True for training while False for testing
            features,sequence_length = denseNet.Dense_net( image, width, isTraining)
            logits = model.rnn_layers(features, sequence_length,
                                      data_queue.num_classes())
            with tf.variable_scope(tf.get_variable_scope(),reuse=False): # purpose here?
                loss= _add_loss(logits, label, sequence_length)
                optimizerOps = _add_optimizer(loss)
                metrics= labelErrors, sequenceErrors = _add_metrics(logits, sequence_length, label, length)

        # trainSummaryOps= tf.get_collection(tf.GraphKeys.SUMMARIES)
        # trainSummaryMerged= tf.summary.merge(trainSummaryOps)
        # valSummaryOps= tf.get_collection(tf.GraphKeys.SUMMARIES, scope= "convnet|rnn|loss|metrics")
        # valSummaryMerged= tf.summary.merge(valSummaryOps, name= "Val_")
        summary= tf.summary.merge_all()
        init_op = tf.group( tf.global_variables_initializer(),
                            tf.local_variables_initializer())

        # sv = tf.train.Supervisor(
        #     logdir=FLAGS.output,
        #     init_op=init_op,
            # init_feed_dict= {isTraining: True},
            # summary_op=summary_op,
            # save_summaries_secs=0,#30
            # init_fn=_get_init_pretrained(),
            # save_model_secs=0)#150
        #
        saver= tf.train.Saver(tf.global_variables(), )
        # summaryWriter= tf.summary.FileWriter(logdir= os.path.join(FLAGS.output, 'test'), graph= tf.get_default_graph(),)
        # coordinator= tf.train.Coordinator()
        session_config = _get_session_config()
        scaffold= tf.train.Scaffold(
            init_op= init_op,
            summary_op= summary,
            saver= saver,
        )
        with tf.train.MonitoredTrainingSession(
                master='',
                checkpoint_dir=  os.path.join(FLAGS.output, 'test'),
                scaffold= scaffold,
                save_checkpoint_secs= 300,
                save_summaries_steps=None,
                save_summaries_secs= 10,
                config=session_config
        ) as sess:
            # sess.run(init_op)

            # threads= tf.train.start_queue_runners(sess= sess, coord= coordinator)

            step = sess.run(global_step)
            while step < FLAGS.max_num_steps:
                if sess.should_stop():
                    break
                [trainLoss, trainMetrics, step]=sess.run(
                    [optimizerOps, metrics, global_step],
                )
                log.debug("step: %s" %(step, ))
                # summaryWriter.add_summary(trainSummary, step)
                #
                # valStep= step * FLAGS.train_val_split
                # if valStep % 1 == 0:
                #     log.info("validation step %s:" %(valStep, ))
                #     [valLoss, valMetrics, valSummary] = sess.run(
                #         [loss, metrics, valSummaryMerged],
                #         feed_dict= {isTraining: False}
                #     )
                #     summaryWriter.add_summary(valSummary, step)
                # print step loss
                if step % 10==0:
                    log.info("Step %+6s: %s." %(step, trainLoss))

            # coordinator.request_stop()
            # coordinator.join(threads)
            saver.save( sess, os.path.join(FLAGS.output,'model.ckpt'), global_step=global_step)


if __name__ == '__main__':
    # _get_input()
    #
    tf.app.run()
