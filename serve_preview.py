import http.server, os
os.chdir(os.path.dirname(__file__))
http.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler, port=7788, bind="127.0.0.1")
