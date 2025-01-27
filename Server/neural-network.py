from flask import Flask, jsonify, request
import pandas as pd
from pandas import ExcelFile
import logging
from logging.handlers import RotatingFileHandler
import numpy as np
import tensorflow as tf
from caffe_classes import class_names
from sklearn.svm import SVC
import pickle
import json
from google.cloud import vision
import cv2
import sys
import json
from scipy.misc import imresize, imsave
from collections import defaultdict

app = Flask(__name__)


@app.route("/")
def hello():
    return "Hello world"


@app.route("/get_attr_name", methods=["GET", "POST"])
def map_attribute_values():
    _id = request.form['id']
    if _id is None:
        raise Exception("Empty id"+str(request.form))
    df = pd.read_excel('nutritionix.xlsx')
    return df.loc[df['attr_id'] == int(_id)].name.to_csv()


@app.route("/nn", methods=["GET", "POST"])
def food_recognition():
    # content is a byte array image
    # dependencies:
    # package: numpy, tensorflow, sklearn, opencv-python, google-cloud-vision
    # files: GoogleCloud.json (to call the API, export GOOGLE_APPLICATION_CREDENTIALS="[path]/GoogleCloud.json" before running)
    # bvlc_alexnet.npy (network weights), caffe_classes.py, namedict.json (food classes), svm.pkl (trained classifier)
    if 'file' in request.files:
        content = request.files['file'].read()
    else:
        raise Exception(
            "Invalid :: Request does not have a image file"+str(request.files))

    # google cloud object detection part
    client = vision.ImageAnnotatorClient()
    image = vision.types.Image(content=content)
    objects = client.object_localization(
        image=image).localized_object_annotations
    d_bbox = dict()

    if(len(objects) <= 0):
        raise Exception("Google Vision bboxes not working")

    for i in range(len(objects)):
        x1 = objects[i].bounding_poly.normalized_vertices[0].x
        y1 = objects[i].bounding_poly.normalized_vertices[0].y
        x2 = objects[i].bounding_poly.normalized_vertices[2].x
        y2 = objects[i].bounding_poly.normalized_vertices[2].y
        # remove duplicates
        if (x1, y1, x2, y2) not in d_bbox:
            d_bbox[(x1, y1, x2, y2)] = (x2-x1)*(y2-y1)

    # tensorflow part
    decoded = cv2.imdecode(np.frombuffer(content, np.uint8), -1)
    s = np.shape(decoded)
    # BGR to RGB
    d1 = (np.zeros(s)).astype(np.uint8)
    d1[:, :, 0] = decoded[:, :, 2]
    d1[:, :, 1] = decoded[:, :, 1]
    d1[:, :, 2] = decoded[:, :, 0]

    patches = list()
    area = list()

    for key in d_bbox:
        x1 = round(s[1]*key[0])
        y1 = round(s[0]*key[1])
        x2 = round(s[1]*key[2])
        y2 = round(s[0]*key[3])
        p = d1[y1:y2, x1:x2, :3]
        imsave(str(x1)+'-'+str(y1)+'.png', p)
        p = imresize(arr=p, size=(227, 227), interp='bilinear')
        p = p[:, :, :3].astype(np.float32)
        p = p-np.mean(p)
        p[:, :, 0], p[:, :, 2] = p[:, :, 2], p[:, :, 0]
        patches.append(p)
        area.append(d_bbox[key])
        area = np.array(area)
        area = area/np.amin(area)
        area = list(area)
        net_data = np.load(open("bvlc_alexnet.npy", "rb"),
                           encoding="latin1").item()
        train_x = np.zeros((1, 227, 227, 3)).astype(np.float32)
        train_y = np.zeros((1, 1000))
        xdim = train_x.shape[1:]
        ydim = train_y.shape[1]

    def conv(input, kernel, biases, k_h, k_w, c_o, s_h, s_w,  padding="VALID", group=1):
        c_i = input.get_shape()[-1]
        assert c_i % group == 0
        assert c_o % group == 0

        def convolve(i, k): return tf.nn.conv2d(
            i, k, [1, s_h, s_w, 1], padding=padding)
        if group == 1:
            conv = convolve(input, kernel)
        else:
            input_groups = tf.split(input, group, 3)
            kernel_groups = tf.split(kernel, group, 3)
            output_groups = [convolve(i, k)
                             for i, k in zip(input_groups, kernel_groups)]
            conv = tf.concat(output_groups, 3)  # tf.concat(3, output_groups)
        return tf.reshape(tf.nn.bias_add(conv, biases), [-1]+conv.get_shape().as_list()[1:])

    x = tf.placeholder(tf.float32, (None,) + xdim)

    # conv1
    # conv(11, 11, 96, 4, 4, padding='VALID', name='conv1')
    k_h = 11
    k_w = 11
    c_o = 96
    s_h = 4
    s_w = 4
    conv1W = tf.Variable(net_data["conv1"][0])
    conv1b = tf.Variable(net_data["conv1"][1])
    conv1_in = conv(x, conv1W, conv1b, k_h, k_w, c_o,
                    s_h, s_w, padding="SAME", group=1)
    conv1 = tf.nn.relu(conv1_in)
    # lrn1
    # lrn(2, 2e-05, 0.75, name='norm1')
    radius = 2
    alpha = 2e-05
    beta = 0.75
    bias = 1.0
    lrn1 = tf.nn.local_response_normalization(conv1,
                                              depth_radius=radius,
                                              alpha=alpha,
                                              beta=beta,
                                              bias=bias)
    # maxpool1
    # max_pool(3, 3, 2, 2, padding='VALID', name='pool1')
    k_h = 3
    k_w = 3
    s_h = 2
    s_w = 2
    padding = 'VALID'
    maxpool1 = tf.nn.max_pool(lrn1, ksize=[1, k_h, k_w, 1], strides=[
                              1, s_h, s_w, 1], padding=padding)
    # conv2
    # conv(5, 5, 256, 1, 1, group=2, name='conv2')
    k_h = 5
    k_w = 5
    c_o = 256
    s_h = 1
    s_w = 1
    group = 2
    conv2W = tf.Variable(net_data["conv2"][0])
    conv2b = tf.Variable(net_data["conv2"][1])
    conv2_in = conv(maxpool1, conv2W, conv2b, k_h, k_w, c_o,
                    s_h, s_w, padding="SAME", group=group)
    conv2 = tf.nn.relu(conv2_in)
    # lrn2
    # lrn(2, 2e-05, 0.75, name='norm2')
    radius = 2
    alpha = 2e-05
    beta = 0.75
    bias = 1.0
    lrn2 = tf.nn.local_response_normalization(conv2,
                                              depth_radius=radius,
                                              alpha=alpha,
                                              beta=beta,
                                              bias=bias)
    # maxpool2
    # max_pool(3, 3, 2, 2, padding='VALID', name='pool2')
    k_h = 3
    k_w = 3
    s_h = 2
    s_w = 2
    padding = 'VALID'
    maxpool2 = tf.nn.max_pool(lrn2, ksize=[1, k_h, k_w, 1], strides=[
                              1, s_h, s_w, 1], padding=padding)
    # conv3
    # conv(3, 3, 384, 1, 1, name='conv3')
    k_h = 3
    k_w = 3
    c_o = 384
    s_h = 1
    s_w = 1
    group = 1
    conv3W = tf.Variable(net_data["conv3"][0])
    conv3b = tf.Variable(net_data["conv3"][1])
    conv3_in = conv(maxpool2, conv3W, conv3b, k_h, k_w, c_o,
                    s_h, s_w, padding="SAME", group=group)
    conv3 = tf.nn.relu(conv3_in)
    # conv4
    # conv(3, 3, 384, 1, 1, group=2, name='conv4')
    k_h = 3
    k_w = 3
    c_o = 384
    s_h = 1
    s_w = 1
    group = 2
    conv4W = tf.Variable(net_data["conv4"][0])
    conv4b = tf.Variable(net_data["conv4"][1])
    conv4_in = conv(conv3, conv4W, conv4b, k_h, k_w, c_o,
                    s_h, s_w, padding="SAME", group=group)
    conv4 = tf.nn.relu(conv4_in)
    # conv5
    # conv(3, 3, 256, 1, 1, group=2, name='conv5')
    k_h = 3
    k_w = 3
    c_o = 256
    s_h = 1
    s_w = 1
    group = 2
    conv5W = tf.Variable(net_data["conv5"][0])
    conv5b = tf.Variable(net_data["conv5"][1])
    conv5_in = conv(conv4, conv5W, conv5b, k_h, k_w, c_o,
                    s_h, s_w, padding="SAME", group=group)
    conv5 = tf.nn.relu(conv5_in)
    # maxpool5
    # max_pool(3, 3, 2, 2, padding='VALID', name='pool5')
    k_h = 3
    k_w = 3
    s_h = 2
    s_w = 2
    padding = 'VALID'
    maxpool5 = tf.nn.max_pool(conv5, ksize=[1, k_h, k_w, 1], strides=[
                              1, s_h, s_w, 1], padding=padding)
    # fc6
    # fc(4096, name='fc6')
    fc6W = tf.Variable(net_data["fc6"][0])
    fc6b = tf.Variable(net_data["fc6"][1])
    fc6 = tf.nn.relu_layer(tf.reshape(
        maxpool5, [-1, int(np.prod(maxpool5.get_shape()[1:]))]), fc6W, fc6b)
    # fc7
    # fc(4096, name='fc7')
    fc7W = tf.Variable(net_data["fc7"][0])
    fc7b = tf.Variable(net_data["fc7"][1])
    fc7 = tf.nn.relu_layer(fc6, fc7W, fc7b)
    # fc8
    # fc(1000, relu=False, name='fc8')
    fc8W = tf.Variable(net_data["fc8"][0])
    fc8b = tf.Variable(net_data["fc8"][1])
    fc8 = tf.nn.xw_plus_b(fc7, fc8W, fc8b)
    # prob
    # softmax(name='prob'))
    prob = tf.nn.softmax(fc8)
    init = tf.initialize_all_variables()
    sess = tf.Session()
    sess.run(init)
    features = sess.run(fc7, feed_dict={x: patches})

    # SVM
    f_svm = open('svm.pkl', 'rb')
    clf = pickle.load(f_svm)
    idx = clf.predict(features)
    f_namedict = open('namedict.json')
    idx2name = json.load(f_namedict)
    names = list()
    for i in range(len(idx)):
        names.append(idx2name[str(int(idx[i]))])
    response = defaultdict(list)
    response["names"] = names
    response["areas"] = area

    return json.dumps(response)


if __name__ == '__main__':
    app.run(debug=True)
    # path = '20151127_132650.jpg'
    # with open(path, 'rb') as image_file:
    #    content = image_file.read()
    # names = food_recognition(content)
    # print(names)
