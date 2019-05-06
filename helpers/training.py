import os
from collections import defaultdict
from operator import itemgetter

import numpy as np
import pandas as pd
from keras import backend as K
from keras_preprocessing.image import ImageDataGenerator
from sklearn.metrics import precision_recall_fscore_support, accuracy_score, confusion_matrix, \
    average_precision_score, mean_absolute_error
from sklearn.model_selection import StratifiedKFold, train_test_split

from helpers.class_activation_map import draw_class_activation_map, crop_and_draw_class_activation_map
from helpers.dataset import get_test_dataset_info, get_train_dataset_info
from helpers.models import train_or_load_model


def get_class_mode(is_binary):
    return "binary" if is_binary else "categorical"


def check_path(filepath):
    parent = os.path.dirname(filepath)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    return filepath


def create_flow(df, is_binary, seed, batch_size, shuffle=True, data_augmentation=False):
    if data_augmentation:
        generator = ImageDataGenerator(rescale=1. / 255, horizontal_flip=True, brightness_range=[0.8, 1.2])
    else:
        generator = ImageDataGenerator(rescale=1. / 255)

    flow = generator.flow_from_dataframe(dataframe=df, directory=None, target_size=(224, 224), shuffle=shuffle,
                                         class_mode=get_class_mode(is_binary), seed=seed, batch_size=batch_size)
    flow.reset()
    return flow


def get_training_and_validation_flow(df, data_augmentation, is_binary, random_seed, batch_size, split_size=0.10):
    x, y = df.iloc[:, 0].values, df.iloc[:, 1].values
    x_train, x_val, y_train, y_val = train_test_split(x, y, test_size=split_size, stratify=y, random_state=random_seed)

    train_data_frame = pd.DataFrame(data={'filename': x_train, 'class': y_train})
    validation_data_frame = pd.DataFrame(data={'filename': x_val, 'class': y_val})

    train_flow = create_flow(train_data_frame, is_binary, random_seed, batch_size, data_augmentation=data_augmentation)
    validation_flow = create_flow(validation_data_frame, is_binary, random_seed, batch_size=1)

    return train_flow, validation_flow


def merge_generators(x1, x2):
    x1.reset()
    x2.reset()

    while True:
        x1i = x1.next()
        x2i = x2.next()
        yield [x1i[0], x2i[0]], x1i[1]


def verify_probabilities(y_probs, train_flow):
    train_indices = train_flow.class_indices
    if train_indices['0'] != 0 and train_flow['1'] != 1:
        return np.array([1. - el for el in y_probs.flatten()])
    return y_probs.flatten()


def calculate_prediction(y_pred_prob, trn_flow, tst_flow, is_binary):
    if is_binary:
        y_pred_prob = verify_probabilities(y_pred_prob, trn_flow)
        y_pred = np.where(y_pred_prob > 0.5, 1, 0)
    else:
        predicted_class_indices = np.argmax(y_pred_prob, axis=1)
        labels = trn_flow.class_indices
        labels = dict((v, k) for k, v in labels.items())
        y_pred = [int(labels[k]) for k in predicted_class_indices]

    return y_pred_prob, y_pred, tst_flow.classes


def calculate_accuracy_per_class(y_test, y_pred):
    cm = confusion_matrix(y_test, y_pred)
    cm = cm.astype('float') / cm.sum(axis=1)[:, np.newaxis]
    print("Accuracy per class ------> {}".format(cm.diagonal()))


def accuracy_precision_recall_fscore(y_test, y_pred, is_binary):
    accuracy = accuracy_score(y_test, y_pred)
    result = {'accuracy': accuracy}

    if is_binary:
        precision, recall, f_score, _ = precision_recall_fscore_support(y_test, y_pred, average='binary')
        result.update({'precision': precision, 'recall': recall, 'f-score': f_score})
    else:
        mean_absolute = mean_absolute_error(y_test, y_pred)
        precision_ma, recall_ma, f_score_ma, _ = precision_recall_fscore_support(y_test, y_pred, average='macro')
        result.update({'precision_ma': precision_ma, 'recall_ma': recall_ma,
                       'f-score_ma': f_score_ma, 'mean_absolute_error': mean_absolute})
    return result


def print_results(metrics, is_binary):
    print("Accuracy ----------------> {}".format(metrics['accuracy']))
    if is_binary:
        print("F-Score -----------------> {}".format(metrics['f-score']))
        print("Precision ---------------> {}".format(metrics['precision']))
        print("Recall ------------------> {}".format(metrics['recall']))
    else:
        print("Mean Absolute Error -----> {}".format(metrics['mean_absolute_error']))
        print("F-Score (macro) ---------> {}".format(metrics['f-score_ma']))
        print("Precision (macro) -------> {}".format(metrics['precision_ma']))
        print("Recall (macro) ----------> {}".format(metrics['recall_ma']))


def print_fold_results(metrics, is_binary):
    if is_binary:
        print("Accuracy ----------------> {}".format(metrics['accuracy']))
        print("F-Score -----------------> {}".format(metrics['f-score']))
    else:
        print("Accuracy ----------------> {}".format(metrics['accuracy']))
        print("F-Score (macro) ---------> {}".format(metrics['f-score_ma']))


def calculate_average_precision_ks(lst, is_binary, ks=(50, 100, 250, 480)):
    if is_binary:
        results, score = sorted(lst, key=itemgetter(0), reverse=True), 0

        for k in ks:
            y_score, y_true = zip(*results[:k])
            y_score, y_true = np.asarray(y_score), np.asarray(y_true)
            average_precision = average_precision_score(y_true, y_score)
            score += average_precision
            print("Average Precision @ {:<3} -> {}".format(k, average_precision))

        print("Average Precision @ {} -> {}".format(ks, score / len(ks)))


def train_test_model_split(args, is_binary, seed, batch_size, epochs):
    filepath = check_path("weights/{}_split/weights.hdf5".format(args['dataset']))

    train_df = get_train_dataset_info(args['dataset'])
    test_df = get_test_dataset_info(args['dataset'])

    trn_flow, val_flow = get_training_and_validation_flow(train_df, args['data_augmentation'], is_binary,
                                                          seed, batch_size, split_size=0.10)
    tst_flow = create_flow(test_df, is_binary, seed, batch_size=1, shuffle=False)

    model = train_or_load_model(args, trn_flow, val_flow, batch_size, filepath, epochs,
                                trn_flow.classes, trn_flow.n, val_flow.n)
    y_pred_prob = model.predict_generator(generator=tst_flow, verbose=1, steps=tst_flow.n)

    y_pred_prob, y_pred, y_test = calculate_prediction(y_pred_prob, trn_flow, tst_flow, is_binary)
    metrics_dict = accuracy_precision_recall_fscore(y_test, y_pred, is_binary)
    y_pred_prob_classes = list(zip(y_pred_prob.tolist(), y_test))

    print_classifications(args, tst_flow, y_pred)
    print_results(metrics_dict, is_binary)

    calculate_average_precision_ks(y_pred_prob_classes, is_binary)
    calculate_accuracy_per_class(y_test, y_pred)

    draw_class_activation_map(model, args, is_binary, test_df, trn_flow)


def train_test_attention_guided_cnn(args, is_binary, seed, batch_size, epochs, nr_folds):
    info = get_train_dataset_info(args['dataset'])
    x, y, = info.iloc[:, 0].values, info.iloc[:, 1].values

    metrics_dict, y_pred_prob_classes, fold_nr = defaultdict(int), [], 1
    k_fold = StratifiedKFold(n_splits=nr_folds, shuffle=True, random_state=seed)

    for train, test in k_fold.split(x, y):
        train_data_frame = pd.DataFrame(data={'filename': x[train], 'class': y[train]})
        test_data_frame = pd.DataFrame(data={'filename': x[test], 'class': y[test]})

        first_branch_path = "weights/{}_attention_guided_global_branch_cv/weights_fold_{}_from_{}.hdf5"
        first_branch_path = check_path(first_branch_path.format(args['dataset'], fold_nr, nr_folds))

        second_branch_path = "weights/{}_attention_guided_local_branch_cv/weights_fold_{}_from_{}.hdf5"
        second_branch_path = check_path(second_branch_path.format(args['dataset'], fold_nr, nr_folds))

        all_network_path = "weights/{}_attention_guided_all_cv/weights_fold_{}_from_{}.hdf5"
        all_network_path = check_path(all_network_path.format(args['dataset'], fold_nr, nr_folds))

        trn_flow_1, val_flow_1 = get_training_and_validation_flow(train_data_frame, args['data_augmentation'],
                                                                  is_binary, seed, batch_size)

        model_global = train_or_load_model(args, trn_flow_1, val_flow_1, batch_size, first_branch_path, epochs,
                                           trn_flow_1.classes, trn_flow_1.n, val_flow_1.n)

        tst_flow_1 = create_flow(test_data_frame, is_binary, seed, batch_size=1, shuffle=False)
        y_pred_prob = model_global.predict_generator(generator=tst_flow_1, verbose=1, steps=tst_flow_1.n)
        y_pred_prob, y_pred, y_test = calculate_prediction(y_pred_prob, trn_flow_1, tst_flow_1, is_binary)
        metrics_it = accuracy_precision_recall_fscore(y_test, y_pred, is_binary)

        print("Global branch results.")
        print_fold_results(metrics_it, is_binary)
        calculate_accuracy_per_class(y_test, y_pred)

        test_data_frame_2 = crop_and_draw_class_activation_map(model_global, args, is_binary,
                                                               test_data_frame, trn_flow_1, fold_nr)

        train_data_frame_2 = crop_and_draw_class_activation_map(model_global, args, is_binary,
                                                                train_data_frame, trn_flow_1, fold_nr)

        trn_flow_2, val_flow_2 = get_training_and_validation_flow(train_data_frame_2, args['data_augmentation'],
                                                                  is_binary, seed, batch_size)
        model_local = train_or_load_model(args, trn_flow_2, val_flow_2, batch_size, second_branch_path, epochs,
                                          trn_flow_2.classes, trn_flow_2.n, val_flow_2.n)

        tst_flow_2 = create_flow(test_data_frame_2, is_binary, seed, batch_size=1, shuffle=False)
        y_pred_prob = model_local.predict_generator(generator=tst_flow_2, verbose=1, steps=tst_flow_2.n)
        y_pred_prob, y_pred, y_test = calculate_prediction(y_pred_prob, trn_flow_2, tst_flow_2, is_binary)
        metrics_it = accuracy_precision_recall_fscore(y_test, y_pred, is_binary)

        print("Local branch results.")
        print_fold_results(metrics_it, is_binary)
        calculate_accuracy_per_class(y_test, y_pred)

        trn_flow_merged = merge_generators(trn_flow_1, trn_flow_2)
        val_flow_merged = merge_generators(val_flow_1, val_flow_2)

        models = {"model_global": model_global, "model_local": model_local}
        model = train_or_load_model(args, trn_flow_merged, val_flow_merged, batch_size, all_network_path,
                                    epochs, trn_flow_1.classes, trn_flow_1.n, val_flow_1.n,
                                    branch="fused", branches_models=models)

        tst_flow_2 = create_flow(test_data_frame_2, is_binary, seed, batch_size=1, shuffle=False)
        tst_flow_merged = merge_generators(tst_flow_1, tst_flow_2)

        y_pred_prob = model.predict_generator(generator=tst_flow_merged, verbose=1, steps=tst_flow_1.n)
        y_pred_prob, y_pred, y_test = calculate_prediction(y_pred_prob, trn_flow_1, tst_flow_1, is_binary)
        metrics_it = accuracy_precision_recall_fscore(y_test, y_pred, is_binary)
        y_pred_prob_classes += list(zip(y_pred_prob.tolist(), y_test))

        print("Fused model results.")
        print_fold_results(metrics_it, is_binary)
        print_classifications(args, tst_flow_1, y_pred)
        calculate_accuracy_per_class(y_test, y_pred)

        draw_class_activation_map(model, args, is_binary, test_data_frame, trn_flow_1)

        metrics_dict = dict((k, metrics_dict[k] + v) for k, v in metrics_it.items())
        K.clear_session()
        fold_nr += 1

    metrics_dict = dict((k, v / nr_folds) for k, v in metrics_dict.items())
    print_results(metrics_dict, is_binary)
    calculate_average_precision_ks(y_pred_prob_classes, is_binary)


def train_test_model_cv(args, is_binary, seed, batch_size, epochs, nr_folds):
    info = get_train_dataset_info(args['dataset'])
    x, y, = info.iloc[:, 0].values, info.iloc[:, 1].values

    metrics_dict, y_pred_prob_classes, fold_nr = defaultdict(int), [], 1
    k_fold = StratifiedKFold(n_splits=nr_folds, shuffle=True, random_state=seed)

    for train, test in k_fold.split(x, y):
        filepath = check_path("weights/{}_cv/weights_fold_{}_from_{}.hdf5".format(args['dataset'], fold_nr, nr_folds))
        train_data_frame = pd.DataFrame(data={'filename': x[train], 'class': y[train]})
        test_data_frame = pd.DataFrame(data={'filename': x[test], 'class': y[test]})

        trn_flow, val_flow = get_training_and_validation_flow(train_data_frame, args['data_augmentation'],
                                                              is_binary, seed, batch_size)
        tst_flow = create_flow(test_data_frame, is_binary, seed, batch_size=1, shuffle=False)

        model = train_or_load_model(args, trn_flow, val_flow, batch_size, filepath, epochs,
                                    trn_flow.classes, trn_flow.n, val_flow.n)
        y_pred_prob = model.predict_generator(generator=tst_flow, verbose=1, steps=tst_flow.n)

        y_pred_prob, y_pred, y_test = calculate_prediction(y_pred_prob, trn_flow, tst_flow, is_binary)
        metrics_it = accuracy_precision_recall_fscore(y_test, y_pred, is_binary)
        y_pred_prob_classes += list(zip(y_pred_prob.tolist(), y_test))

        print_fold_results(metrics_it, is_binary)
        print_classifications(args, tst_flow, y_pred)
        calculate_accuracy_per_class(y_test, y_pred)
        draw_class_activation_map(model, args, is_binary, test_data_frame, trn_flow)

        metrics_dict = dict((k, metrics_dict[k] + v) for k, v in metrics_it.items())
        K.clear_session()
        fold_nr += 1

    metrics_dict = dict((k, v / nr_folds) for k, v in metrics_dict.items())
    print_results(metrics_dict, is_binary)
    calculate_average_precision_ks(y_pred_prob_classes, is_binary)


def print_classifications(args, tst_flow, y_pred):
    if not args['print_classifications']:
        return

    with open("info.txt", "a+") as f:
        for idx, el in enumerate(y_pred):
            green, red, end = '\033[92m', '\033[91m', '\033[0m'
            color = green if tst_flow.classes[idx] == el else red
            print(color + "{:<105} -> True: {} | Pred: {}"
                  .format(tst_flow.filenames[idx], tst_flow.classes[idx], el) + end)
            tick = "Correct" if tst_flow.classes[idx] == el else "Incorrect"
            f.write("{:<105} -> True: {} | Pred: {} -> {}\n"
                    .format(tst_flow.filenames[idx], tst_flow.classes[idx], el, tick))
        f.write("------------------------------------------------\n")
