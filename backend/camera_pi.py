import os
import io
import cv2
import time
import glob
import base64
import numpy as np
from celery import Celery
from functools import reduce
from datetime import datetime, timedelta
from importlib import import_module
from picamera.array import PiRGBArray
from picamera import PiCamera
from dotenv import load_dotenv
from backend.centroidtracker import CentroidTracker
from backend.base_camera import BaseCamera
from backend.utils import reduce_tracking

load_dotenv()
Detector = import_module('backend.' + os.environ['DETECTION_MODEL']).Detector

WIDTH = 640
HEIGHT = 480
IMAGE_FOLDER = "./imgs"

celery = Celery("app")
celery.conf.update(
        broker_url='redis://localhost:6379/0',
        result_backend='redis://localhost:6379/0',
        beat_schedule={
            "photos_SO": {
                "task": "backend.camera_pi.CaptureContinous",
                "schedule": timedelta(
                    seconds=int(str(os.environ['BEAT_INTERVAL']))
                    ),
                "args": []
                }
            }
)


class Camera(BaseCamera):
    @staticmethod
    def frames():
        with PiCamera() as camera:
            camera.rotation = int(str(os.environ['CAMERA_ROTATION']))
            stream = io.BytesIO()
            for _ in camera.capture_continuous(stream, 'jpeg',
                                               use_video_port=True):
                # return current frame
                stream.seek(0)
                _stream = stream.getvalue()
                data = np.fromstring(_stream, dtype=np.uint8)
                img = cv2.imdecode(data, 1)
                yield img

                # reset stream for next frame
                stream.seek(0)
                stream.truncate()


class Predictor(object):
    """Docstring for Predictor. """

    def __init__(self):
        self.detector = Detector()
        self.ct = CentroidTracker(maxDisappeared=20)

    def prediction(self, img, conf_th=0.3, conf_class=[]):
        output = self.detector.prediction(img)
        df = self.detector.filter_prediction(output, img, conf_th=conf_th, conf_class=conf_class)
        img = self.detector.draw_boxes(img, df)
        return img

    def object_track(self, img, conf_th=0.3, conf_class=[]):
        output = self.detector.prediction(img)
        df = self.detector.filter_prediction(output, img, conf_th=conf_th, conf_class=conf_class)
        img = self.detector.draw_boxes(img, df)
        boxes = df[['x1', 'y1', 'x2', 'y2']].values
        objects = self.ct.update(boxes)
        if len(boxes) > 0 and (df['class_name'].str.contains('person').any()):
            for (objectID, centroid) in objects.items():
                text = "ID {}".format(objectID)
                cv2.putText(img, text, (centroid[0] - 10, centroid[1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                cv2.circle(img, (centroid[0], centroid[1]), 4, (0, 255, 0), -1)
        return img

    def img_to_base64(self, img):
        """encode as a jpeg image and return it"""
        buffer = cv2.imencode('.jpg', img)[1].tobytes()
        jpg_as_text = base64.b64encode(buffer)
        base64_string = jpg_as_text.decode('utf-8')
        return base64_string


@celery.task(bind=True)
def CaptureContinous(self):
    detector = Detector()
    with PiCamera() as camera:
        camera.resolution = (1280, 960)  # twice height and widht
        camera.rotation = int(str(os.environ['CAMERA_ROTATION']))
        camera.framerate = 10
        with PiRGBArray(camera, size=(WIDTH, HEIGHT)) as output:
            camera.capture(output, 'bgr', resize=(WIDTH, HEIGHT))
            image = output.array
            result = detector.prediction(image)
            df = detector.filter_prediction(result, image)
            if len(df) > 0:
                if (df['class_name']
                        .str
                        .contains('person|bird|cat|wine glass|cup|sandwich')
                        .any()):
                    day = datetime.now().strftime("%Y%m%d")
                    directory = os.path.join(IMAGE_FOLDER, 'pi', day)
                    if not os.path.exists(directory):
                        os.makedirs(directory)
                    image = detector.draw_boxes(image, df)
                    classes = df['class_name'].unique().tolist()
                    hour = datetime.now().strftime("%H%M%S")
                    filename_output = os.path.join(
                            directory,
                            "{}_{}_.jpg".format(hour, "-".join(classes))
                            )
                    cv2.imwrite(filename_output, image)

@celery.task(bind=True)
def ObjectTracking(self):
    detector = Detector()
    myiter = glob.iglob(os.path.join(IMAGE_FOLDER, '**', '*.jpg'),
                        recursive=True)
    newdict = reduce(lambda a, b: reduce_tracking(a,b), myiter, dict())
    startID = max(map(int, newdict.keys()), default=0) + 1
    ct = CentroidTracker(startID=startID)
    with PiCamera() as camera:
        camera.resolution = (1280, 960)  # twice height and widht
        camera.rotation = int(str(os.environ['CAMERA_ROTATION']))
        camera.framerate = 10
        with PiRGBArray(camera, size=(WIDTH, HEIGHT)) as output:
            while True:
                camera.capture(output, 'bgr', resize=(WIDTH, HEIGHT))
                img = output.array
                result = detector.prediction(img)
                df = detector.filter_prediction(result, img)
                img = detector.draw_boxes(img, df)
                boxes = df[['x1', 'y1', 'x2', 'y2']].values
                previous_object_ID = ct.nextObjectID
                objects = ct.update(boxes)
                if len(boxes) > 0 and (df['class_name'].str.contains('person').any()) and previous_object_ID in list(objects.keys()):
                    for (objectID, centroid) in objects.items():
                        text = "ID {}".format(objectID)
                        cv2.putText(img, text, (centroid[0] - 10, centroid[1] - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                        cv2.circle(img, (centroid[0], centroid[1]), 4, (0, 255, 0), -1)

                    day = datetime.now().strftime("%Y%m%d")
                    directory = os.path.join(IMAGE_FOLDER, 'pi', day)
                    if not os.path.exists(directory):
                        os.makedirs(directory)
                    ids = "-".join(list([str(i) for i in objects.keys()]))
                    hour = datetime.now().strftime("%H%M%S")
                    filename_output = os.path.join(
                            directory, "{}_person_{}_.jpg".format(hour, ids)
                            )
                    cv2.imwrite(filename_output, img)
                time.sleep(0.300)

if __name__ == '__main__':
    CaptureContinous()
