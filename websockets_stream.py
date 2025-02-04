import io
import requests
import tornado.httpserver
import tornado.websocket
import tornado.concurrent
import tornado.ioloop
import tornado.web
import tornado.gen
import threading
import asyncio
import socket
import numpy as np
import imutils
import copy
import time
import cv2
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
import shutil
import glob
bytes = b''

lock = threading.Lock()
connectedDevices = set()
load_dotenv()
streaming_status = {"is_streaming": False}
video_storage_path = "videos"
os.makedirs(video_storage_path, exist_ok=True)
video_writers = {}
video_writer = None
video_filename = None
frames = []

# Configure Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)
class WSHandler(tornado.websocket.WebSocketHandler):
    def __init__(self, *args, **kwargs):
        super(WSHandler, self).__init__(*args, **kwargs)
        self.outputFrame = None
        self.frame = None
        self.id = None
        self.executor = tornado.concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.video_writer = None
        self.is_recording = False
        self.video_filename = None
        # self.stopEvent = threading.Event()

    def process_frames(self):
        if self.frame is None:
            return
        frame = imutils.rotate_bound(self.frame.copy(), 90)
        (flag, encodedImage) = cv2.imencode(".jpg", frame)

        # ensure the frame was successfully encoded
        if not flag:
            return
        self.outputFrame = encodedImage.tobytes()
        if self.is_recording and self.video_writer:
            self.video_writer.write(self.frame)
            #print(frame)
            print(f"Recording frame for device {self.id}")

    def open(self):
        print('new connection')
        connectedDevices.add(self)
        # self.t = threading.Thread(target=self.process_frames)
        # self.t.daemon = True
        # self.t.start()

    def on_message(self, message):
        if self.id is None:
            self.id = message
        else:
            self.frame = cv2.imdecode(np.frombuffer(
                message, dtype=np.uint8), cv2.IMREAD_COLOR)
            # self.process_frames()
            tornado.ioloop.IOLoop.current().run_in_executor(self.executor, self.process_frames)

    def start_recording(self):
        if self.frame is None:
            print(f"No frame available for device {self.id}. Cannot start recording.")
            return
        self.video_filename = os.path.join(video_storage_path, f"{self.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.avi")
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        (h, w) = self.frame.shape[:2]
        self.video_writer = cv2.VideoWriter(self.video_filename, fourcc, 20.0, (w, h))
        self.is_recording = True
        print(f"Started recording for device {self.id}")

    def stop_recording(self):
        if self.video_writer and self.is_recording:
            self.is_recording = False
            self.video_writer.release()
            self.video_writer = None
            print(f"Stopped recording for device {self.id}")
            self.upload_video_to_cloudinary()

    def upload_video_to_cloudinary(self):
        if self.video_filename and os.path.exists(self.video_filename):
            try:
                response = cloudinary.uploader.upload_large(
                    self.video_filename, resource_type="video"
                )
                video_url = response.get("secure_url")
                print(f"Video uploaded successfully: {video_url}")
                os.remove(self.video_filename)
            except Exception as e:
                print(f"Failed to upload video: {e}")

    def on_close(self):
        print('connection closed')
        # self.stopEvent.set()
        connectedDevices.remove(self)
        self.stop_recording()

    def check_origin(self, origin):
        return True


class StreamHandler(tornado.web.RequestHandler):
    @tornado.gen.coroutine
    def get(self, slug):
        self.set_header(
            'Cache-Control', 'no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0')
        self.set_header('Pragma', 'no-cache')
        self.set_header(
            'Content-Type', 'multipart/x-mixed-replace;boundary=--jpgboundary')
        self.set_header('Connection', 'close')

        my_boundary = "--jpgboundary"
        client = None
        for c in connectedDevices:
            if c.id == slug:
                print(slug)
                client = c
                break
        try:
            while client is not None:
                jpgData = client.outputFrame
                if jpgData is None:
                    print("empty frame")
                    break
                self.write(my_boundary)
                self.write("Content-type: image/jpeg\r\n")
                self.write("Content-length: %s\r\n\r\n" % len(jpgData))
                self.write(jpgData)
                yield self.flush()
        except tornado.iostream.StreamClosedError:
            print(f"Stream closed for {slug}")
        except Exception as e:
            print(f"Error in StreamHandler: {e}")

class ButtonHandler(tornado.web.RequestHandler):
    def get(self):
        # Capture the current frame
        frame = None
        for client in connectedDevices:
            if client.outputFrame is not None:
                frame = client.outputFrame
                break

        if frame is None:
            self.write("No frame available.")
            return

        # Send the frame to the server at backendsmarthome.vercel.app
        url = "https://backendsmarthome.vercel.app/notify"

        # Prepare the image for sending
        files = {
            'image': ('frame.jpg', io.BytesIO(frame), 'image/jpeg')
        }

        try:
            # Make the HTTP request to the external server
            response = requests.post(url, files=files)
            if response.status_code == 200:
                self.write("Frame sent successfully.")
            else:
                self.write(f"Failed to send frame. Status code: {response.status_code}")
        except Exception as e:
            self.write(f"Error while sending frame: {str(e)}")

    #def get(self):
     #   self.write("This is a POST-only endpoint.")


class TemplateHandler(tornado.web.RequestHandler):
    def get(self):
        deviceIds = [d.id for d in connectedDevices]
        self.render(os.path.join(os.path.dirname(__file__), "templates", "index.html"), url=f"{os.getenv('URL')}/video_feed/", deviceIds=deviceIds)
        # self.render(os.path.sep.join(
        #     [os.path.dirname(__file__), "templates", "index.html"]), url=f"{os.getenv('URL')}/video_feed/", deviceIds=deviceIds)
class CaptureHandler(tornado.web.RequestHandler):
    def post(self):
        if not connectedDevices:
            self.write("No connected devices.")
            return

        # Gửi lệnh "capture" đến tất cả các ESP32 đã kết nối
        for client in connectedDevices:
            client.write_message("capture")
            client.start_recording()
        streaming_status["is_streaming"] = True
        self.write("Sent 'capture' command to ESP32.")
class StopCaptureHandler(tornado.web.RequestHandler):
    def post(self):
        if not connectedDevices:
            self.write("No connected devices.")
            return

        # Gửi lệnh "stop_capture" đến tất cả các ESP32 đã kết nối
        for client in connectedDevices:
            client.write_message("stop_capture")
            client.stop_recording()
        streaming_status["is_streaming"] = False
        self.write("Sent 'stop_capture' command to ESP32.")

class StatusHandler(tornado.web.RequestHandler):
    def get(self):
        global streaming_status
        self.write({"status": "streaming" if streaming_status["is_streaming"] else "stopped"})

class VideoHandler:
    @staticmethod
    def cleanup_old_videos():
        files = glob.glob(os.path.join(video_storage_path, "*.mp4"))
        total_size = sum(os.path.getsize(f) for f in files) / (1024 * 1024 * 1024)
        max_storage = 5  # GB
        max_days = 2
        now = datetime.now()
        for file in files:
            file_time = datetime.fromtimestamp(os.path.getmtime(file))
            if total_size > max_storage or (now - file_time).days > max_days:
                os.remove(file)
                total_size -= os.path.getsize(file) / (1024 * 1024 * 1024)

    @staticmethod
    def get_video_list():
        files = glob.glob(os.path.join(video_storage_path, "*.mp4"))
        files.sort(key=os.path.getmtime, reverse=True)
        return [os.path.basename(f) for f in files]

class VideoAPIHandler(tornado.web.RequestHandler):
    def get(self):
        videos = VideoHandler.get_video_list()
        self.write({"videos": videos})

class DeleteVideoHandler(tornado.web.RequestHandler):
    def post(self):
        video_name = self.get_argument("video_name", None)
        if not video_name:
            self.write({"error": "Missing video_name parameter"})
            return
        video_path = os.path.join(video_storage_path, video_name)
        if os.path.exists(video_path):
            os.remove(video_path)
            self.write({"message": "Video deleted successfully"})
        else:
            self.write({"error": "Video not found"})

application = tornado.web.Application([
    (r'/video_feed/([^/]+)', StreamHandler),
    (r'/ws', WSHandler),
    (r'/button', ButtonHandler),
    (r'/stream', CaptureHandler),  # Route mới để gửi lệnh "capture"
    (r'/stop_capture', StopCaptureHandler),  # Route mới để tắt capture
    (r"/status", StatusHandler),
    (r'/videos', VideoAPIHandler),  # API để lấy danh sách video
    (r'/delete_video', DeleteVideoHandler),  # API để xóa video
    (r'/', TemplateHandler),
    (r'/videos/(.*)', tornado.web.StaticFileHandler, {'path': video_storage_path}),
])


if __name__ == "__main__":
    http_server = tornado.httpserver.HTTPServer(application)
    http_server.listen(os.getenv("PORT"))
    myIP = socket.gethostbyname(socket.gethostname())
    print('*** Websocket Server Started at %s***' % myIP)
    tornado.ioloop.IOLoop.current().start()






