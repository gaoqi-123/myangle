import sqlite3
import random
import uuid
import os
from flask import Flask, render_template, request, redirect, url_for, flash

# --- 应用配置 ---
app = Flask(__name__)
# ！！！请务必在实际部署时更改此密钥！！！
app.secret_key = 'your_very_strong_and_unique_secret_key_12345'
DATABASE = 'database.db'


# --- 数据库操作函数 ---

def get_db():
    """获取数据库连接"""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row  # 允许通过列名访问数据
    return conn


def init_db():
    """初始化数据库表结构"""
    with app.app_context():
        db = get_db()
        # rooms 表: 存储房间信息 (ID, 名称, 人数上限, 状态)
        db.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_id TEXT PRIMARY KEY,
                room_name TEXT NOT NULL,
                target_count INTEGER NOT NULL,
                status TEXT NOT NULL
            );
        """)
        # participants 表: 存储玩家信息 (名字, 查询码, 匹配结果)
        db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                name TEXT NOT NULL,
                secret_code TEXT NOT NULL,
                target_name TEXT,
                FOREIGN KEY (room_id) REFERENCES rooms(room_id)
            );
        """)
        db.commit()


# --- 核心匹配逻辑 ---

def perform_matching(participants):
    """执行去环随机排列（Derangement）匹配，确保 A != Guard(A)"""
    names = [p['name'] for p in participants]
    max_attempts = 100
    for _ in range(max_attempts):
        targets = names[:]
        random.shuffle(targets)

        # 检查是否满足 A != Guard(A) 的条件
        is_valid = all(names[i] != targets[i] for i in range(len(names)))

        if is_valid:
            matching = {names[i]: targets[i] for i in range(len(names))}
            return matching

    return None


# --- 路由定义 ---

@app.route('/', methods=['GET', 'POST'])
def create_room():
    """页面 1: 房间创建和链接分享"""
    if request.method == 'POST':
        room_name = request.form['room_name']
        try:
            target_count = int(request.form['target_count'])
            if target_count < 2:
                flash("参与人数至少需要 2 人。", 'danger')
                return redirect(url_for('create_room'))
        except ValueError:
            flash("请输入有效的数字作为人数。", 'danger')
            return redirect(url_for('create_room'))

        room_id = str(uuid.uuid4())[:8]

        db = get_db()
        db.execute("INSERT INTO rooms (room_id, room_name, target_count, status) VALUES (?, ?, ?, ?)",
                   (room_id, room_name, target_count, 'OPEN'))
        db.commit()

        join_url = url_for('join_room', room_id=room_id, _external=True)
        return render_template('page1_create.html', join_url=join_url, room_name=room_name)

    return render_template('page1_create.html')


@app.route('/join/<room_id>', methods=['GET', 'POST'])
def join_room(room_id):
    """页面 2: 玩家登记、名单显示与结果查询"""
    db = get_db()
    room = db.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()

    if not room:
        flash("房间不存在或链接错误！", 'danger')
        return redirect(url_for('create_room'))

    # 获取当前已登记的玩家名单 (只获取名字)
    participants_rows = db.execute("SELECT name FROM participants WHERE room_id = ?", (room_id,)).fetchall()
    participants_names = [p['name'] for p in participants_rows]
    current_count = len(participants_names)

    # --- POST: 处理登记与查询操作 ---
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'register':
            # **1. 登记操作**
            name = request.form['name'].strip()
            secret_code = request.form['secret_code'].strip()

            # 后端强制唯一性校验：确保名字未被使用
            existing_participant = db.execute(
                "SELECT id FROM participants WHERE room_id = ? AND name = ?",
                (room_id, name)
            ).fetchone()

            if not name or not secret_code:
                flash("名字和查询码不能为空。", 'danger')
            elif existing_participant:
                # 名字已存在，拒绝重复登记，但提示用户已登记成功
                flash(f"名字 '{name}' 已存在。您已登记成功，请勿重复操作。", 'warning')
            elif room['status'] != 'OPEN':
                flash("房间已完成匹配，无法再加入。", 'danger')
            elif current_count >= room['target_count']:
                flash("房间人数已满，无法再加入。", 'danger')
            else:
                # 名字未存在，执行插入操作
                db.execute("INSERT INTO participants (room_id, name, secret_code) VALUES (?, ?, ?)",
                           (room_id, name, secret_code))
                db.commit()
                flash(f"登记成功！您是第 {current_count + 1} 位玩家。", 'success')

                # 准备检查是否触发匹配
                new_count = current_count + 1

                # **2. 自动触发匹配**
                if new_count == room['target_count']:
                    new_participants = db.execute("SELECT * FROM participants WHERE room_id = ?", (room_id,)).fetchall()
                    matching_result = perform_matching(new_participants)

                    if matching_result:
                        # 更新匹配结果和房间状态
                        for p in new_participants:
                            db.execute("UPDATE participants SET target_name = ? WHERE id = ?",
                                       (matching_result[p['name']], p['id']))
                        db.execute("UPDATE rooms SET status = ? WHERE room_id = ?", ('MATCHED', room_id))
                        db.commit()
                        flash("人数已满，匹配成功自动完成！🎉 您现在可以查询结果了。", 'info')
                    else:
                        flash("匹配失败，请联系发起人重试。", 'danger')

                # 关键：成功登记后重定向，并携带名字作为参数，供前端设置 Local Storage 标记
                return redirect(url_for('join_room', room_id=room_id, registered_name=name))

        elif action == 'query':
            # **3. 结果查询操作 (三重安全校验)**
            query_name = request.form['query_name'].strip()
            query_code = request.form['query_code'].strip()

            if room['status'] != 'MATCHED':
                flash("匹配尚未开始，请等待所有玩家登记完毕。", 'warning')
            elif not query_name or not query_code:
                flash("请完整输入名字和查询码。", 'danger')
            else:
                # 核心：根据 Room ID, 名字和查询码进行校验
                result = db.execute(
                    "SELECT target_name FROM participants WHERE room_id = ? AND name = ? AND secret_code = ?",
                    (room_id, query_name, query_code)).fetchone()

                if result and result['target_name']:
                    # 校验成功
                    flash(f"🎉 您的守护对象是：**{result['target_name']}**", 'success')
                else:
                    # 校验失败 (统一提示，不泄露哪个字段错误)
                    flash("查询失败：名字或查询码不正确。", 'danger')

            return redirect(url_for('join_room', room_id=room_id))

    # --- GET: 页面渲染 ---
    # 重新查询最新的参与者名单以显示
    participants_names = [p['name'] for p in
                          db.execute("SELECT name FROM participants WHERE room_id = ?", (room_id,)).fetchall()]
    return render_template('page2_join.html',
                           room=room,
                           participants_names=participants_names,
                           current_count=len(participants_names))


# --- 应用启动 ---
if __name__ == '__main__':
    # 确保数据库存在并初始化
    init_db()
    # 运行应用
    app.run(debug=True)