import logging
import threading
import time
import queue
import builtins
import os
import random
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

# 导入业务逻辑
import main
import browser
import email_service
from config import cfg

app = Flask(__name__, static_url_path='')

# ==========================================
# 📁 文件路径工具
# ==========================================
def resolve_accounts_file_path() -> str:
    path = cfg.files.accounts_file
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(__file__), path)

# ==========================================
# 🔧 状态管理与日志捕获
# ==========================================

# ==========================================
# 🔧 状态管理与日志捕获
# ==========================================

# 全局状态
class AppState:
    def __init__(self):
        self.is_running = False
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "等待启动"
        self.logs = []
        self.lock = threading.Lock()
        
        # MJPEG 流缓冲区
        self.last_frame = None 
        self.frame_lock = threading.Lock()

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{timestamp}] {message}")
            if len(self.logs) > 1000:
                self.logs.pop(0)

    def get_logs(self, start_index=0):
        with self.lock:
            return list(self.logs[start_index:])
            
    def update_frame(self, frame_bytes):
        with self.frame_lock:
            self.last_frame = frame_bytes
            
    def get_frame(self):
        with self.frame_lock:
            return self.last_frame

state = AppState()

# Hack: 劫持 print 函数以捕获日志
original_print = builtins.print
def hooked_print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    msg = sep.join(map(str, args))
    state.add_log(msg)
    original_print(*args, **kwargs)

# 应用劫持
main.print = hooked_print
browser.print = hooked_print
email_service.print = hooked_print

# ==========================================
# 🧵 后台工作线程
# ==========================================
def worker_thread(count):
    state.is_running = True
    state.stop_requested = False
    state.success_count = 0
    state.fail_count = 0
    state.current_action = f"🚀 任务启动，目标: {count}"
    
    # 清空上一轮的画面，避免显示残留
    state.update_frame(None)
    
    main.print(f"🚀 开始批量任务，计划注册: {count} 个")
    
    try:
        def monitor(driver, step):
            # 1. 检查是否请求停止
            if state.stop_requested:
                main.print("🛑 检测到停止请求，正在中断任务...")
                raise InterruptedError("用户请求停止")
            
            # 2. 截图更新流 (MJPEG)
            try:
                # 获取 PNG 字节流 (内存操作，极快)
                png_bytes = driver.get_screenshot_as_png()
                state.update_frame(png_bytes)
            except Exception as e:
                main.print(f"⚠️ 截图流更新失败: {e}")

        for i in range(count):
            if state.stop_requested:
                main.print("🛑 用户停止了任务")
                break
            
            state.current_action = f"正在注册 ({i+1}/{count})..."
            
            try:
                # 调用核心逻辑，传入回调
                email, password, success = main.register_one_account(monitor_callback=monitor)
                
                if success:
                    state.success_count += 1
                else:
                    state.fail_count += 1
            except InterruptedError:
                main.print("🛑 任务已中断")
                break
            except Exception as e:
                state.fail_count += 1
                main.print(f"❌ 异常: {str(e)}")
            
            # 间隔等待
            if i < count - 1 and not state.stop_requested:
                wait_time = random.randint(cfg.batch.interval_min, cfg.batch.interval_max)
                main.print(f"⏳ 冷却中，等待 {wait_time} 秒...")
                for _ in range(wait_time):
                    if state.stop_requested: break
                    time.sleep(1)
                    
    except Exception as e:
        main.print(f"💥 严重错误: {e}")
    finally:
        state.is_running = False
        state.current_action = "任务已完成"
        main.print("🏁 任务结束")

# ==========================================
# 🌊 MJPEG 流生成器
# ==========================================
def gen_frames():
    """生成流数据的生成器"""
    while True:
        frame = state.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + frame + b'\r\n')
        else:
            # 如果没有画面（例如刚启动），可以发送一个空帧或者只是等待
            pass
            
        time.sleep(0.5) # 控制刷新率，避免浏览器过于频繁请求

@app.route('/video_feed')
def video_feed():
    return Flask.response_class(gen_frames(),
                               mimetype='multipart/x-mixed-replace; boundary=frame')

# ==========================================
# 🌐 API 接口
# ==========================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/status')
def get_status():
    # 获取库存数
    total_inventory = 0
    accounts_path = resolve_accounts_file_path()
    if os.path.exists(accounts_path):
        try:
            with open(accounts_path, 'r', encoding='utf-8', errors='replace') as f:
                total_inventory = sum(1 for line in f if '@' in line)
        except:
            pass

    return jsonify({
        "is_running": state.is_running,
        "current_action": state.current_action,
        "success": state.success_count,
        "fail": state.fail_count,
        "total_inventory": total_inventory,
        "logs": state.get_logs(int(request.args.get('log_index', 0)))
    })

@app.route('/api/start', methods=['POST'])
def start_task():
    if state.is_running:
        return jsonify({"error": "Already running"}), 400
    
    data = request.json
    count = data.get('count', 1)
    
    threading.Thread(target=worker_thread, args=(count,), daemon=True).start()
    return jsonify({"status": "started"})

@app.route('/api/stop', methods=['POST'])
def stop_task():
    if not state.is_running:
        return jsonify({"error": "Not running"}), 400
    
    state.stop_requested = True
    return jsonify({"status": "stopping"})

@app.route('/api/accounts')
def get_accounts():
    def normalize_time_str(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        try:
            return datetime.strptime(value, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value

    def parse_account_line(line: str) -> dict | None:
        raw = (line or "").strip()
        if not raw or raw.startswith("#"):
            return None

        if "|" in raw:
            parts = [p.strip() for p in raw.split("|", maxsplit=3)]
            if len(parts) < 2:
                return None
            email = parts[0]
            if "@" not in email:
                return None
            return {
                "email": email,
                "password": parts[1],
                "status": parts[2] if len(parts) > 2 else "",
                "time": normalize_time_str(parts[3] if len(parts) > 3 else ""),
            }

        if "----" in raw:
            parts = [p.strip() for p in raw.split("----", maxsplit=3)]
            if len(parts) < 2:
                return None
            email = parts[0]
            if "@" not in email:
                return None
            return {
                "email": email,
                "password": parts[1],
                "status": parts[3] if len(parts) > 3 else "",
                "time": normalize_time_str(parts[2] if len(parts) > 2 else ""),
            }

        return None

    accounts = []
    accounts_path = resolve_accounts_file_path()
    if os.path.exists(accounts_path):
        try:
            with open(accounts_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    parsed = parse_account_line(line)
                    if parsed:
                        accounts.append(parsed)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # 反转列表，最新的在前
    return jsonify(accounts[::-1])

if __name__ == '__main__':
    from waitress import serve
    print("🌐 Web Server started at http://localhost:5001")
    # 使用生产级服务器 Waitress
    # threads=6 支持并发：前端页面 + API轮询 + MJPEG流 + 后台任务
    serve(app, host='0.0.0.0', port=5001, threads=6)
