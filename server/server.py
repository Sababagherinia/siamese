from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

from warnings import simplefilter

simplefilter(action="ignore", category=FutureWarning)
import sys
# import signal
import pathlib
import time
import configparser
import cv2
import numpy as np
import socket
import tensorflow as tf
from uuid import uuid1
from datetime import datetime
from sklearn import preprocessing

# recognition
from recognition.utils import load_model, parse_status, Timer
from recognition.preprocessing import normalize_input, cvt_to_gray
from recognition.distance import bulk_cosine_similarity, bulk_cosine_similarity_v2
from recognition.utils import reshape_image

# face detection
from face_detection.detector import FaceDetector
from face_detection.mtcnn import detect_face

# database
from database.sync import parse_person_id_dictionary
from database.component import ImageDatabase

# motion
from motion_detection.component import BSMotionDetection

# tracker
from tracker.policy import Policy, PolicyTracker
from tracker import TrackerList, MATCHED
from tracker.tracklet.component import TrackLet
from tracker.tracklet.component import TrackerContainer

# HPE
from hpe import HeadPoseEstimation

# tools
from tools.logger import Logger
from stream.source import OpencvSource, MultiSource

# setting
from settings import (COLOR_DANG,
                      COLOR_SUCCESS,
                      TRACKER_CONF,
                      SUMMARY_LOG_DIR,
                      SOURCE_CONF,
                      HPE_CONF)
from settings import (BASE_DIR,
                      GALLERY_CONF,
                      DEFAULT_CONF,
                      MODEL_CONF,
                      CAMERA_MODEL_CONF,
                      DETECTOR_CONF,
                      SERVER_CONF,
                      IMAGE_CONF)

# serializer
from .serializer import face_serializer

# logs
from v2.tools.logger import LOG_Path, FileLogger


# signal
# from .signals import control_c_signal_handler


def recognition_serve(args):
    """
    argument from arg parser
    :param args:
    :return:
    """
    log_subdir = SUMMARY_LOG_DIR.joinpath(datetime.strftime(datetime.now(), '%Y-%m-%d(%H-%M-%S)'))
    logger = Logger(log_dir=log_subdir)
    if not log_subdir.exists():
        log_subdir.mkdir(parents=True)

    physical_devices = tf.config.list_physical_devices('GPU')
    logger.info("$ On {}".format(physical_devices[0].name))
    tf.config.experimental.set_memory_growth(physical_devices[0], True)

    logger.info(f"$ {parse_status(args)} recognition mode ...")

    conf = configparser.ConfigParser()
    conf.read(os.path.join(BASE_DIR, "conf.ini"))
    model_conf = conf['Model']
    detector_conf = conf['Detector']
    gallery_conf = conf['Gallery']
    default_conf = conf['Default']

    database = ImageDatabase(db_path=gallery_conf.get("database_path"))

    if (args.realtime or args.video) and args.eval_method == "cosine":
        if not database.is_db_stable():
            logger.info("$ database is not stable, build npy file or no one is registered")
            sys.exit()
        else:
            logger.info("$ database is stable")
        logger.info("$ loading embeddings ...")
        embeds, labels = database.bulk_embeddings()
        encoded_labels = preprocessing.LabelEncoder()
        encoded_labels.fit(list(set(labels)))
        labels = encoded_labels.transform(labels)
    motion_detection = BSMotionDetection()

    with tf.device('/device:gpu:0'):
        with tf.Graph().as_default():
            with tf.compat.v1.Session() as sess:
                logger.info(f"$ Initializing computation graph with {model_conf.get('facenet')} pretrained model.")
                load_model(os.path.join(BASE_DIR, model_conf.get('facenet')))
                logger.info("$ Model has been loaded.")
                input_plc = tf.compat.v1.get_default_graph().get_tensor_by_name("input:0")
                embeddings = tf.compat.v1.get_default_graph().get_tensor_by_name("embeddings:0")
                phase_train = tf.compat.v1.get_default_graph().get_tensor_by_name("phase_train:0")

                detector = FaceDetector(sess=sess)
                detector_type = detector_conf.get("type")
                logger.info(f"$ {detector_type} face detector has been loaded.")

                _source = 0
                _source_name = ""
                if args.realtime and args.proto == "rtsp":
                    _source = SOURCE_CONF.get("cam_1")
                    _source_name = "rtsp_video_cam_1"
                elif args.video_file:
                    _source = args.video_file
                    _source_name = "local_video_cam"
                cap = OpencvSource(src=_source, name=_source_name, width=int(CAMERA_MODEL_CONF.get("width")),
                                   height=int(CAMERA_MODEL_CONF.get("height")))

                frame_width = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
                frame_height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)

                if not args.cluster:
                    tk = TrackerList(float(TRACKER_CONF.get("max_modify_time")),
                                     int(TRACKER_CONF.get("max_frame_conf")))
                prev = 1
                proc_timer = Timer()
                while cap.isOpened():
                    proc_timer.start()
                    ret, frame = cap.read()
                    cur = time.time()
                    delta_time = cur - prev
                    if not ret:
                        break

                    if (delta_time > 1. / float(detector_conf['fps'])) and (
                            motion_detection.has_motion(frame) is not None or args.cluster):

                        prev = cur
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        if not args.cluster:
                            faces = list()

                        boxes = []
                        gray_frame = cvt_to_gray(frame) if not args.cluster else frame
                        for face, bbox in detector.extract_faces(gray_frame, frame_width, frame_height):
                            faces.append(normalize_input(face))
                            boxes.append(bbox)

                        if not args.cluster:
                            faces = np.array(faces)

                        if (args.video or args.realtime) and (faces.shape[0] > 0):
                            feed_dic = {phase_train: False, input_plc: faces}
                            embedded_array = sess.run(embeddings, feed_dic)

                            if args.eval_method == 'cosine':
                                dists = bulk_cosine_similarity(embedded_array, embeds)
                                bs_similarity_idx = np.argmin(dists, axis=1)
                                bs_similarity = dists[np.arange(len(bs_similarity_idx)), bs_similarity_idx]
                                pred_labels = np.array(labels)[bs_similarity_idx]
                                for i in range(len(pred_labels)):
                                    x, y, w, h = boxes[i]
                                    status = encoded_labels.inverse_transform([pred_labels[i]]) if bs_similarity[
                                                                                                       i] < float(
                                        default_conf.get("similarity_threshold")) else ['unrecognised']
                                    if status[0] == "unrecognised":
                                        color = COLOR_DANG
                                    else:
                                        res = tk(name=status)
                                        if res == MATCHED:
                                            color = COLOR_SUCCESS
                                        else:
                                            color = COLOR_SUCCESS
                                            status = [""]


                            else:
                                break

                            tk.modify()


def recognition_serv_2(args):
    # database
    database = ImageDatabase(db_path=GALLERY_CONF.get("database_path"))
    embeds, labels = database.bulk_embeddings()
    encoded_labels = preprocessing.LabelEncoder()
    encoded_labels.fit(list(set(labels)))
    labels = encoded_labels.transform(labels)
    person_ids = parse_person_id_dictionary()

    # memory growth
    physical_devices = tf.config.list_physical_devices('GPU')
    tf.config.experimental.set_memory_growth(physical_devices[0], True)

    tracker = PolicyTracker(max_life_time=float(TRACKER_CONF.get("max_modify_time")),
                            max_conf=int(TRACKER_CONF.get("max_frame_conf")))

    global_unrecognized_cnt = 0

    address = (SERVER_CONF.get("UDP_HOST"), int(SERVER_CONF.get("UDP_PORT")))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    face_save_path = pathlib.Path(SERVER_CONF.get("face_save_path")).joinpath(SERVER_CONF.get("face_folder"))
    if not face_save_path.exists():
        face_save_path.mkdir(parents=True)

    # computation graph
    with tf.device('/device:gpu:0'):
        with tf.Graph().as_default():
            with tf.compat.v1.Session() as sess:
                load_model(os.path.join(BASE_DIR, MODEL_CONF.get('facenet')))
                input_plc = tf.compat.v1.get_default_graph().get_tensor_by_name("input:0")
                embeddings = tf.compat.v1.get_default_graph().get_tensor_by_name("embeddings:0")
                phase_train = tf.compat.v1.get_default_graph().get_tensor_by_name("phase_train:0")

                # face detector
                detector = FaceDetector(sess=sess)

                _source = 0
                _source_name = ""
                if args.realtime and args.proto == "rtsp":
                    _source = SOURCE_CONF.get("cam_1")
                    _source_name = "rtsp_video_cam_1"

                cap = OpencvSource(src=_source, name=_source_name, width=int(CAMERA_MODEL_CONF.get("width")),
                                   height=int(CAMERA_MODEL_CONF.get("height")))

                prev = 1
                while cap.isOpened():
                    cur = time.time()
                    delta_time = cur - prev

                    ret, frame = cap.read()

                    if not ret:
                        break

                    if delta_time > 1. / float(DETECTOR_CONF['fps']):
                        prev = cur

                        serial_event = []

                        frame_ = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        gray_frame = cvt_to_gray(frame_)

                        boxes = []
                        faces = []
                        for face, bbox in detector.extract_faces(gray_frame, int(CAMERA_MODEL_CONF.get("width")),
                                                                 int(CAMERA_MODEL_CONF.get("height"))):
                            faces.append(normalize_input(face))
                            boxes.append(bbox)

                        faces = np.array(faces)

                        cvt_frame = cv2.cvtColor(frame_.copy(), cv2.COLOR_RGB2BGR)

                        if faces.shape[0] > 0:
                            feed_dic = {phase_train: False, input_plc: faces}
                            embedded_array = sess.run(embeddings, feed_dic)
                            dists = bulk_cosine_similarity(embedded_array, embeds)
                            bs_similarity_idx = np.argmin(dists, axis=1)
                            bs_similarity = dists[np.arange(len(bs_similarity_idx)), bs_similarity_idx]
                            pred_labels = np.array(labels)[bs_similarity_idx]
                            for i in range(len(pred_labels)):
                                uu_ = uuid1()
                                file_name_ = uu_.hex + ".jpg"
                                save_path = face_save_path.joinpath(file_name_)
                                x, y, w, h = boxes[i]
                                status = encoded_labels.inverse_transform([pred_labels[i]]) if bs_similarity[
                                                                                                   i] < float(
                                    DEFAULT_CONF.get("similarity_threshold")) else ['unrecognised']

                                print(status[0])

                                try:

                                    if status[0] == "unrecognised":
                                        if global_unrecognized_cnt == int(TRACKER_CONF.get("unrecognized_counter")):
                                            now_ = datetime.now()
                                            serial_ = face_serializer(timestamp=int(now_.timestamp()) * 1000,
                                                                      person_id=None,
                                                                      camera_id=None,
                                                                      image_path=file_name_)

                                            serial_event.append(serial_)
                                            cv2.imwrite(str(save_path), cvt_frame[y:y + h, x:x + w])
                                            global_unrecognized_cnt = 0
                                        else:
                                            global_unrecognized_cnt += 1

                                    else:
                                        tk_ = tracker(name=status[0])

                                        if tk_.status == Policy.STATUS_CONF and not tk_.mark and status[0]:
                                            tk_.mark = True
                                            now_ = datetime.now()
                                            id_ = person_ids.get(status[0])

                                            if id_ is not None:
                                                serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                                          person_id=id_,
                                                                          camera_id=None,
                                                                          image_path=file_name_)

                                                # serial_ = face_serializer(timestamp=now_.timestamp(),
                                                #                           person_id=None,
                                                #                           camera_id=None,
                                                #                           image_path=str(save_path))

                                                serial_event.append(serial_)

                                                cv2.imwrite(str(save_path), cvt_frame[y:y + h, x:x + w])

                                except:
                                    print("Record Drop")

                        if serial_event:
                            json_obj = json.dumps({"data": serial_event})
                            sock.sendto(json_obj.encode(), address)
                            print(json_obj + " Send to " + f"{address[0]}:{address[1]}")


def recognition_track_let_serv(args):
    # image size
    debug_mode = args.debug
    log_path = LOG_Path.joinpath("server")
    log_path.mkdir(parents=True, exist_ok=True)
    _cu = datetime.strftime(datetime.now(), '%Y%m%d-%H%M%S')

    file_logger = FileLogger(log_path=log_path.joinpath(f"{_cu}.log"), name="server")
    output_size = (int(IMAGE_CONF.get('width')), int(IMAGE_CONF.get('height')))

    # logger
    log_subdir = SUMMARY_LOG_DIR.joinpath(datetime.strftime(datetime.now(), '%Y-%m-%d(%H-%M-%S)'))
    logger = Logger(log_dir=log_subdir)
    if not log_subdir.exists():
        log_subdir.mkdir(parents=True)

    # database
    database = ImageDatabase(db_path=GALLERY_CONF.get("database_path"))
    embeds, labels = database.bulk_embeddings()
    encoded_labels = preprocessing.LabelEncoder()
    encoded_labels.fit(list(set(labels)))
    labels = encoded_labels.transform(labels)
    person_ids = parse_person_id_dictionary()

    # memory growth
    physical_devices = tf.config.list_physical_devices('GPU')
    tf.config.experimental.set_memory_growth(physical_devices[0], True)

    # sender
    address = (SERVER_CONF.get("UDP_HOST"), int(SERVER_CONF.get("UDP_PORT")))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # define face saving path
    face_save_path = pathlib.Path(SERVER_CONF.get("face_save_path")).joinpath(SERVER_CONF.get("face_folder"))
    if not face_save_path.exists():
        face_save_path.mkdir(parents=True)

    # trackers
    tracker = PolicyTracker(max_life_time=float(TRACKER_CONF.get("recognized_max_modify_time")),
                            max_conf=int(TRACKER_CONF.get("recognized_max_frame_conf")))

    unrecognized_tracker = PolicyTracker(max_life_time=float(TRACKER_CONF.get("unrecognized_max_modify_time")),
                                         max_conf=int(TRACKER_CONF.get("unrecognized_max_frame_conf")))

    interval = int(DETECTOR_CONF.get("mtcnn_per_frame"))

    track_let = TrackLet(0.9, interval,
                         iou_threshold=float(TRACKER_CONF.get("iou_threshold")),
                         max_age=int(TRACKER_CONF.get("max_age")),
                         min_hints=int(TRACKER_CONF.get("min_hits")))

    tracker_min_conf = int(TRACKER_CONF.get("min_conf_frame"))
    tracker_container = TrackerContainer(max_track_id=int(TRACKER_CONF.get("min_tracked_id")))

    # HPE

    hpe_conf = (
        float(HPE_CONF.get("pan_left")),
        float(HPE_CONF.get("pan_right")),
        float(HPE_CONF.get("tilt_up")),
        float(HPE_CONF.get("tilt_down"))
    )

    hpe = HeadPoseEstimation(
        img_norm=(float(HPE_CONF.get("im_norm_mean")), float(HPE_CONF.get("im_norm_var"))),
        tilt_norm=(float(HPE_CONF.get("tilt_norm_mean")), float(HPE_CONF.get("tilt_norm_var"))),
        pan_norm=(float(HPE_CONF.get("pan_norm_mean")), float(HPE_CONF.get("pan_norm_var"))),
        rescale=float(HPE_CONF.get("rescale")),
        conf=hpe_conf
    )

    cnt = 0
    msg = None
    if database.is_db_stable():
        msg = "[Stable] Database is stable"
        file_logger.info(msg)
        logger.success(msg)
        stable_mode = True
    else:
        msg = "[Not Stable] Database is not stable, build npy or register someone!"
        file_logger.info(msg)
        logger.dang(msg)
        stable_mode = False

    # computation graph
    with tf.device('/device:gpu:0'):
        with tf.Graph().as_default():
            with tf.compat.v1.Session() as sess:

                # recognition computation graph
                load_model(os.path.join(BASE_DIR, MODEL_CONF.get('facenet')))
                input_plc = tf.compat.v1.get_default_graph().get_tensor_by_name("input:0")
                embeddings = tf.compat.v1.get_default_graph().get_tensor_by_name("embeddings:0")
                phase_train = tf.compat.v1.get_default_graph().get_tensor_by_name("phase_train:0")

                pnet, rnet, onet = detect_face.create_mtcnn(sess)

                # hpe computation graph
                load_model(os.path.join(BASE_DIR, HPE_CONF.get("model")))

                hpe_input = tf.compat.v1.get_default_graph().get_tensor_by_name("x:0")
                hpe_output = tf.compat.v1.get_default_graph().get_tensor_by_name("Identity:0")

                # face detector computation graph

                minsize = int(DETECTOR_CONF.get("min_face_size"))
                threshold = [
                    float(DETECTOR_CONF.get("step1_threshold")),
                    float(DETECTOR_CONF.get("step2_threshold")),
                    float(DETECTOR_CONF.get("step3_threshold"))]
                factor = float(DETECTOR_CONF.get("scale_factor"))

                x_margin = int(DETECTOR_CONF.get("x_margin"))
                y_margin = int(DETECTOR_CONF.get("y_margin"))

                _source = 0
                _source_name = ""
                if args.realtime and args.proto == "rtsp":
                    _source = SOURCE_CONF.get("cam_1")
                    _source_name = "rtsp_video_cam_1"

                cap = MultiSource(src=_source, name=_source_name, width=int(CAMERA_MODEL_CONF.get("width")),
                                  height=int(CAMERA_MODEL_CONF.get("height")))

                watch_list = []
                msg = "[OK] Ready to start"
                file_logger.info(msg)
                logger.success(msg)
                while True:

                    try:
                        ret, frame = cap.read()

                        if not ret:
                            msg = "[Failure] Frame is not retrieved from source camera"
                            file_logger.info(msg)
                            logger.dang(msg)
                            continue

                        serial_event = []

                        frame_ = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        ch1_fr = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                        gray_frame = cvt_to_gray(frame_)

                        faces = []
                        points = []

                        if cnt % interval == 0:
                            faces, points = detect_face.detect_face(frame_, minsize, pnet, rnet, onet, threshold,
                                                                    factor)

                        # expiration deletion ( recognized )
                        ex_lists = list(tracker.get_expires())

                        get_aliases = tracker.get_aliases(ex_lists)

                        for exp_cont in get_aliases:
                            exp_cont.sort(key=lambda pol: pol.counter, reverse=True)
                            if len(exp_cont) > 1:
                                names = [c.name for c in exp_cont]

                                msg = f"[EXPIRE] Tracker With ID {exp_cont[0].alias_name} With Name/Names {names}"
                                file_logger.info(msg)
                                if debug_mode:
                                    logger.warn(msg)

                                first_gt_pol = exp_cont[0]
                                sec_gt_pol = exp_cont[1]

                                if first_gt_pol.counter == sec_gt_pol.counter:
                                    if tracker_container.validate(first_gt_pol.alias_name):
                                        now_ = datetime.now()

                                        serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                                  person_id=None,
                                                                  camera_id=None,
                                                                  image_path=first_gt_pol.filename,
                                                                  confidence=None)

                                        serial_event.append(serial_)

                                        msg = f"----> Choose Unknown"
                                        file_logger.info(msg)
                                        if debug_mode:
                                            logger.warn(msg)

                                    else:
                                        msg = f"[NO REACH] Tracker With ID {first_gt_pol.alias_name} Can`t reach the " \
                                              f"quorum "
                                        file_logger.info(msg)
                                        if debug_mode:
                                            logger.warn(msg)

                                elif not first_gt_pol.mark and first_gt_pol.alias_name:
                                    if first_gt_pol.alias_name in watch_list:
                                        watch_list.remove(first_gt_pol.alias_name)
                                    else:
                                        if first_gt_pol.counter > tracker_min_conf and tracker_container.validate(
                                                first_gt_pol.alias_name):
                                            now_ = datetime.now()
                                            # id_ = person_ids.get(first_gt_pol.name)
                                            score = float(first_gt_pol.counter / int(
                                                TRACKER_CONF.get("recognized_max_frame_conf")))

                                            serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                                      person_id=first_gt_pol.name,
                                                                      camera_id=None,
                                                                      image_path=first_gt_pol.filename,
                                                                      confidence=round(score * 100, 2))
                                            serial_event.append(serial_)

                                            msg = f"----> Choose {first_gt_pol.name}-> Counter: {first_gt_pol.counter} " \
                                                  f"Confidence: {first_gt_pol.confidence} Sent: No"
                                            file_logger.info(msg)
                                            if debug_mode:
                                                logger.warn(msg)

                                        else:
                                            if tracker_container.validate(n_id=first_gt_pol.alias_name):
                                                now_ = datetime.now()

                                                serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                                          person_id=None,
                                                                          camera_id=None,
                                                                          image_path=first_gt_pol.filename,
                                                                          confidence=None)

                                                serial_event.append(serial_)

                                                msg = f"----> Choose Unknown"
                                                file_logger.info(msg)
                                                if debug_mode:
                                                    logger.warn(msg)

                                            else:
                                                msg = f"[NO REACH] Tracker With ID {first_gt_pol.alias_name} Can`t reach " \
                                                      f"the quorum"
                                                file_logger.info(msg)
                                                if debug_mode:
                                                    logger.warn(msg)

                                unrecognized_tracker.drop_with_alias_name(first_gt_pol.alias_name)
                                tracker.drop_with_alias_name(first_gt_pol.alias_name)

                            elif 0 < len(exp_cont) <= 1:
                                ex_tk = exp_cont[0]

                                if not ex_tk.mark:
                                    if ex_tk.counter > tracker_min_conf and tracker_container.validate(
                                            n_id=ex_tk.alias_name):
                                        now_ = datetime.now()
                                        id_ = person_ids.get(ex_tk.name)
                                        score = float(ex_tk.counter / int(
                                            TRACKER_CONF.get("recognized_max_frame_conf")))

                                        serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                                  person_id=ex_tk.name,
                                                                  camera_id=None,
                                                                  image_path=ex_tk.filename,
                                                                  confidence=round(score * 100, 2))
                                        serial_event.append(serial_)

                                        msg = f"[EXPIRE] Tracker With ID {ex_tk.alias_name} With Name/Names {ex_tk.name}-> " \
                                              f"Counter: {ex_tk.counter} Confidence: {ex_tk.confidence} Sent: No"
                                        file_logger.info(msg)
                                        if debug_mode:
                                            logger.warn(msg)

                                    else:
                                        if tracker_container.validate(n_id=ex_tk.alias_name):
                                            now_ = datetime.now()

                                            serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                                      person_id=None,
                                                                      camera_id=None,
                                                                      image_path=ex_tk.filename,
                                                                      confidence=None)

                                            serial_event.append(serial_)

                                            msg = f"[EXPIRE] Tracker With ID {ex_tk.alias_name} With Name/Names " \
                                                  f"unknown-> Counter: {ex_tk.counter} Confidence: {ex_tk.confidence}"
                                            file_logger.info(msg)
                                            if debug_mode:
                                                logger.warn(msg)

                                        else:
                                            msg = f"[NO REACH] Tracker With ID {ex_tk.alias_name} Can`t reach the " \
                                                  f"quorum"
                                            file_logger.info(msg)
                                            if debug_mode:
                                                logger.warn(msg)

                                else:
                                    msg = f"[EXPIRE] Tracker With ID {ex_tk.alias_name} With Name/Names {ex_tk.name}-> " \
                                          f"Counter: {ex_tk.counter} Confidence: {ex_tk.confidence} Sent: Yes"
                                    file_logger.info(msg)
                                    if debug_mode:
                                        logger.warn(msg)

                                unrecognized_tracker.drop_with_alias_name(ex_tk.alias_name)
                                tracker.drop_with_alias_name(ex_tk.alias_name)

                        # expiration deletion (unrecognized)
                        ex_lists = list(unrecognized_tracker.get_expires())
                        for exp_ in ex_lists:
                            if not exp_.mark:
                                if exp_.counter > tracker_min_conf and tracker_container.validate(exp_.alias_name):
                                    now_ = datetime.now()

                                    serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                              person_id=None,
                                                              camera_id=None,
                                                              image_path=exp_.filename,
                                                              confidence=None)

                                    serial_event.append(serial_)

                                    msg = f"[EXPIRE] Unrecognized Tracker With ID {exp_.alias_name} With Name/Names " \
                                          f"unknown-> Counter: {exp_.counter} Sent: No"
                                    file_logger.info(msg)
                                    if debug_mode:
                                        logger.warn(msg)

                                else:
                                    msg = f"[NO REACH] Unrecognized Tracker With ID {exp_.alias_name} Can`t reach the " \
                                          f"quorum"
                                    file_logger.info(msg)
                                    if debug_mode:
                                        logger.warn(msg)

                            else:

                                msg = f"[EXPIRE] Unrecognized Tracker With ID {exp_.alias_name} -> " \
                                      f"Counter: {exp_.counter} Sent: Yes"
                                file_logger.info(msg)
                                if debug_mode:
                                    logger.warn(msg)

                        frame_size = frame.shape

                        tracks = track_let.detect(faces, frame, points, frame_size)
                        tracks = tracks.astype(np.int32)

                        cnt += 1

                        final_bounding_box = []
                        final_status = []

                        tracks_bounding_box_to = []
                        tracks_face_to = []
                        tracks_status_to = []

                        for tk in tracks:
                            bounding_box = tk[0:4]
                            id_ = str(tk[4])

                            res = tracker.search_alias_name(id_)

                            if res is not None:
                                if res[1].status == Policy.STATUS_CONF:
                                    res[1]()
                                    tracker_container(n_id=res[1].alias_name)
                                    final_bounding_box.append(bounding_box)
                                    final_status.append(res[1].name)
                                    __tilt, __pan = res[1].angle

                                    msg = f"[OK] +Recognized Tracker ID {res[1].alias_name} With Name {res[1].name}-> " \
                                          f"Counter {res[1].counter} Confidence {round(float(res[1].confidence), 3)} " \
                                          f"Bounding Box {bounding_box} " \
                                          f"Tilt {round(float(__tilt), 3)} " \
                                          f"Pan {round(float(__pan), 3)}"

                                    file_logger.info(msg)
                                    if debug_mode:
                                        logger.info(msg, white=True)

                                    # print("Result", res[1].name, sep=" ")
                                else:
                                    tracks_bounding_box_to.append(bounding_box)
                                    tracks_status_to.append(id_)
                                    tracks_face_to.append(
                                        normalize_input(reshape_image(gray_frame, bounding_box, output_size)))
                            else:
                                tracks_bounding_box_to.append(bounding_box)
                                tracks_status_to.append(id_)

                                tracks_face_to.append(
                                    normalize_input(reshape_image(gray_frame, bounding_box, output_size)))

                        tracks_bounding_box_to = np.array(tracks_bounding_box_to)
                        tracks_face_to = np.array(tracks_face_to)
                        tracks_status_to = np.array(tracks_status_to)

                        # HPE

                        cropped_images = hpe.get_cropped_pics(ch1_fr, tracks_bounding_box_to, 0, "large")
                        poses = hpe.predict(sess, cropped_images, hpe_input, hpe_output)
                        right_pose_idx = hpe.validate_angle_bulk(po=poses)
                        com_pos_idx = hpe.validate_angle_bulk_complement(po=poses)

                        # Drop
                        if com_pos_idx.shape[0] > 0:
                            for c_idx in com_pos_idx:
                                msg = f"[DROP] Face With Tilt {poses[c_idx, 0]} Pan {poses[c_idx, 1]}"

                                file_logger.info(msg)
                                if debug_mode:
                                    logger.dang(msg)

                        if tracks_face_to.shape[0] > 0 and right_pose_idx.shape[0] > 0:
                            # update

                            tracks_bounding_box_to = tracks_bounding_box_to[right_pose_idx, :]
                            tracks_face_to = tracks_face_to[right_pose_idx, :, :, :]
                            tracks_status_to = tracks_status_to[right_pose_idx]
                            poses = poses[right_pose_idx, :]

                            if stable_mode:
                                feed_dic = {phase_train: False, input_plc: tracks_face_to}
                                embedded_array = sess.run(embeddings, feed_dic)
                                if args.eval_method == "cosine":
                                    dists = bulk_cosine_similarity(embedded_array, embeds)
                                elif args.eval_method == "cosine_v2":
                                    dists = bulk_cosine_similarity_v2(embedded_array, embeds)
                                else:
                                    logger.dang("[Invalid] Invalid metric")
                                    time.sleep(2)
                                    sys.exit(0)

                                bs_similarity_idx = np.argmin(dists, axis=1)
                                bs_similarity = dists[np.arange(len(bs_similarity_idx)), bs_similarity_idx]
                                pred_labels = np.array(labels)[bs_similarity_idx]
                                for i in range(len(pred_labels)):
                                    uu_ = uuid1()
                                    file_name_ = uu_.hex + ".jpg"
                                    save_path = face_save_path.joinpath(file_name_)
                                    x1, y1, x2, y2 = tracks_bounding_box_to[i, 0], tracks_bounding_box_to[i, 1], \
                                                     tracks_bounding_box_to[i, 2], tracks_bounding_box_to[i, 3]

                                    x1_t, y1_t, x2_t, y2_t = x1, y1, x2, y2

                                    x1, y1, x2, y2 = cap.convert_coordinate([x1, y1, x2, y2],
                                                                            margin=(x_margin, y_margin))

                                    status = encoded_labels.inverse_transform([pred_labels[i]]) if bs_similarity[
                                                                                                       i] < float(
                                        DEFAULT_CONF.get("similarity_threshold")) else ['unrecognised']
                                    tk_status = tracks_status_to[i]

                                    try:

                                        if status[0] == "unrecognised":
                                            tk_, _ = unrecognized_tracker(name=tk_status, alias_name=tk_status)
                                            tk_.angle = (poses[i, 0], poses[i, 1])
                                            tk_.confidence = bs_similarity[i]
                                            tracker_container(n_id=tk_.alias_name)

                                            msg = f"[OK] -Recognized Tracker ID {tk_.alias_name} With Name " \
                                                  f"Unknown -> Counter {tk_.counter} " \
                                                  f"Confidence {round(float(tk_.confidence), 3)} " \
                                                  f"Bounding Box {[x1_t, y1_t, x2_t, y2_t]} " \
                                                  f"Tilt {round(float(poses[i, 0]), 3)} " \
                                                  f"Pan {round(float(poses[i, 1]), 3)}"

                                            file_logger.info(msg)
                                            if debug_mode:
                                                logger.info(msg, white=True)

                                            tk_.save_image(cap.original_frame[y1:y2, x1:x2], face_save_path)
                                            if tk_.status == Policy.STATUS_CONF and not tk_.mark and status[0]:
                                                # print(f"=> Unrecognized with id {tk_status} {bs_similarity[i]}")
                                                tk_.mark = True
                                                now_ = datetime.now()
                                                serial_ = face_serializer(timestamp=int(now_.timestamp()) * 1000,
                                                                          person_id=None,
                                                                          camera_id=None,
                                                                          image_path=file_name_,
                                                                          confidence=None)

                                                serial_event.append(serial_)
                                                cv2.imwrite(str(save_path), cap.original_frame[y1:y2, x1:x2])

                                        else:
                                            tk_, _ = tracker(name=status[0], alias_name=tk_status)
                                            tracker_container(n_id=tk_.alias_name)
                                            tk_.angle = (poses[i, 0], poses[i, 1])
                                            tk_.confidence = bs_similarity[i]

                                            msg = f"[OK] -Recognized Tracker ID {tk_.alias_name} With Name {tk_.name}-> " \
                                                  f"Counter {tk_.counter} Confidence {round(float(tk_.confidence), 3)} " \
                                                  f"Bounding Box {[x1_t, y1_t, x2_t, y2_t]} " \
                                                  f"Tilt {round(float(poses[i, 0]), 3)} " \
                                                  f"Pan {round(float(poses[i, 1]), 3)}"

                                            file_logger.info(msg)
                                            if debug_mode:
                                                logger.info(msg, white=True)

                                            tk_.save_image(cap.original_frame[y1:y2, x1:x2], face_save_path)

                                            if tk_.status == Policy.STATUS_CONF and not tk_.mark and status[0]:
                                                tk_.mark = True
                                                now_ = datetime.now()
                                                # id_ = person_ids.get(status[0])
                                                #
                                                # if id_ is not None:
                                                serial_ = face_serializer(timestamp=int(now_.timestamp() * 1000),
                                                                          person_id=status[0],
                                                                          camera_id=None,
                                                                          image_path=file_name_,
                                                                          confidence=100.)

                                                serial_event.append(serial_)

                                                cv2.imwrite(str(save_path), cap.original_frame[y1:y2, x1:x2])

                                                unrecognized_tracker.drop_with_alias_name(tk_.alias_name)
                                                tracker.drop_exe_name_with_alias(name=status[0], tk_id=tk_.alias_name)
                                                watch_list.append(tk_.alias_name)

                                    except:
                                        logger.warn("Record Drop")

                            else:
                                for i in range(tracks_bounding_box_to.shape[0]):
                                    uu_ = uuid1()
                                    file_name_ = uu_.hex + ".jpg"
                                    save_path = face_save_path.joinpath(file_name_)
                                    x1, y1, x2, y2 = tracks_bounding_box_to[i, 0], tracks_bounding_box_to[i, 1], \
                                                     tracks_bounding_box_to[i, 2], tracks_bounding_box_to[i, 3]

                                    x1_t, y1_t, x2_t, y2_t = x1, y1, x2, y2

                                    x1, y1, x2, y2 = cap.convert_coordinate([x1, y1, x2, y2],
                                                                            margin=(x_margin, y_margin))

                                    status = "unrecognised"
                                    tk_status = tracks_status_to[i]
                                    tk_, _ = unrecognized_tracker(name=tk_status, alias_name=tk_status)
                                    tk_.angle = (poses[i, 0], poses[i, 1])
                                    tracker_container(n_id=tk_.alias_name)

                                    msg = f"[OK] -Recognized Tracker ID {tk_.alias_name} With Name " \
                                          f"Unknown -> Counter {tk_.counter} Confidence {tk_.confidence} " \
                                          f"Bounding Box {[x1_t, y1_t, x2_t, y2_t]} "

                                    file_logger.info(msg)
                                    if debug_mode:
                                        logger.info(msg, white=True)

                                    if tk_.status == Policy.STATUS_CONF and not tk_.mark and status[0]:
                                        # print(f"=> Unrecognized with id {tk_status} {bs_similarity[i]}")
                                        tk_.mark = True
                                        now_ = datetime.now()
                                        serial_ = face_serializer(timestamp=int(now_.timestamp()) * 1000,
                                                                  person_id=None,
                                                                  camera_id=None,
                                                                  image_path=file_name_,
                                                                  confidence=None)

                                        serial_event.append(serial_)
                                        cv2.imwrite(str(save_path), cap.original_frame[y1:y2, x1:x2])

                        if serial_event:
                            json_obj = json.dumps({"data": serial_event})
                            sock.sendto(json_obj.encode(), address)

                            msg = "[SEND] " + f"{address[0]}:{address[1]} " + json_obj

                            file_logger.info(msg)
                            if debug_mode:
                                logger.warn(msg)

                    except KeyboardInterrupt:
                        msg = "[Shutdown] Server is Shutting down"
                        file_logger.info(msg)
                        logger.warn(msg)
                        sys.exit(0)
