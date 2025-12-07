import random
import uuid
import os
import psycopg2
import psycopg2.extras # 用于实现类似 sqlite3.Row 的字典式访问
from flask import Flask, render_template, request, redirect, url_for, flash

# --- 应用配置 ---
app = Flask(__name__)
# 从环境变量获取密钥。如果未设置 (本地测试时)，则使用默认值。
# 注意：在本地运行时，如果使用 python-dotenv，请在 app.py 顶部添加 load_dotenv()
app.secret_key = os.environ.get('SECRET_KEY', 'your_long_random_local_test_key_12345') 

# 从环境变量获取数据库 URL
DATABASE_URL = os.environ.get('DATABASE_URL')
# ---

# --- 数据库操作函数 ---

def get_db():
    """获取 PostgreSQL 数据库连接"""
    if not DATABASE_URL:
        # 如果在生产环境 (Render) 中未设置 DATABASE_URL，则抛出错误
        raise ValueError("DATABASE_URL 环境变量未设置！请检查 Render 配置或本地 .env 文件。") 
        
    try:
        # 连接到 PostgreSQL 数据库
        conn = psycopg2.connect(DATABASE_URL)
        # 使用 DictCursor，使查询结果可以通过列名访问
        conn.cursor_factory = psycopg2.extras.DictCursor 
        return conn
    except psycopg2.Error as e:
        print(f"Database connection error: {e}")
        flash("数据库连接失败，请联系管理员。", 'danger')
        raise e

def init_db():
    """初始化 PostgreSQL 数据库表结构"""
    db = get_db()
    cursor = db.cursor()

    try:
        # rooms 表: 存储房间信息
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_id VARCHAR(8) PRIMARY KEY,
                room_name VARCHAR(255) NOT NULL,
                target_count INTEGER NOT NULL,
                status VARCHAR(10) NOT NULL
            );
        """)
        
        # participants 表: 存储玩家信息
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                id SERIAL PRIMARY KEY,
                room_id VARCHAR(8) NOT NULL REFERENCES rooms(room_id),
                name VARCHAR(255) NOT NULL,
                secret_code VARCHAR(255) NOT NULL,
                target_name VARCHAR(255)
            );
        """)
        db.commit()
    except psycopg2.Error as e:
        print(f"Database initialization error: {e}")
        db.rollback() 
    finally:
        cursor.close()
        db.close()

# --- 核心匹配逻辑 (已添加特殊规则) ---

def perform_matching(participants):
    """
    执行匹配，并包含特殊规则：
    如果 '欢欢' 和 '高奇' 都在，则强制：'高奇' -> '欢欢'。
    """
    all_names = [p['name'] for p in participants]
    
    # 定义特殊规则
    RULE_GUARDIAN = '高奇'
    RULE_TARGET = '欢欢'
    
    # 检查两个关键成员是否都存在
    is_special_rule_active = RULE_GUARDIAN in all_names and RULE_TARGET in all_names
    
    matching = {}
    remaining_guardians = all_names[:] # 剩余需要分配守护对象的
    remaining_targets = all_names[:]   # 剩余可以被守护的对象
    
    # 1. 强制执行规则 (如果激活)
    if is_special_rule_active:
        # 强制执行规则：高奇守护欢欢
        matching[RULE_GUARDIAN] = RULE_TARGET
        
        # 将这两个角色从后续的随机匹配池中移除
        remaining_guardians.remove(RULE_GUARDIAN) # 高奇已分配守护对象，从守护者池中移除
        remaining_targets.remove(RULE_TARGET)     # 欢欢已是目标，不能再被随机分配为目标

    # 2. 对剩余成员进行随机匹配
    
    # 如果只剩高奇和欢欢（总共2人），则直接返回规则匹配结果
    if not remaining_guardians:
        return matching
        
    # 执行去环匹配（Derangement）
    max_attempts = 100 
    
    for _ in range(max_attempts):
        targets = remaining_targets[:] 
        random.shuffle(targets) 
        
        # 检查去环条件：剩余守护者不能匹配到自己
        is_valid = True
        
        # 验证所有剩余的守护者都没有匹配到自己
        for i in range(len(remaining_guardians)):
            if remaining_guardians[i] == targets[i]:
                is_valid = False
                break
        
        if is_valid:
            # 找到有效的随机匹配
            for i in range(len(remaining_guardians)):
                matching[remaining_guardians[i]] = targets[i]
            
            return matching
    
    # 达到最大尝试次数仍失败
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
        cursor = db.cursor()
        
        try:
            # PostgreSQL 占位符使用 %s
            cursor.execute("INSERT INTO rooms (room_id, room_name, target_count, status) VALUES (%s, %s, %s, %s)",
                       (room_id, room_name, target_count, 'OPEN'))
            db.commit()
        except psycopg2.Error as e:
            flash("数据库写入失败，请检查数据库状态。", 'danger')
            db.rollback()
            return redirect(url_for('create_room'))
        finally:
            cursor.close()
            db.close()
        
        join_url = url_for('join_room', room_id=room_id, _external=True)
        return render_template('page1_create.html', join_url=join_url, room_name=room_name)
    
    return render_template('page1_create.html')

@app.route('/join/<room_id>', methods=['GET', 'POST'])
def join_room(room_id):
    """页面 2: 玩家登记、名单显示与结果查询"""
    db = get_db()
    cursor = db.cursor()
    
    # 查找房间信息
    cursor.execute("SELECT * FROM rooms WHERE room_id = %s", (room_id,))
    room = cursor.fetchone()

    if not room:
        cursor.close()
        db.close()
        flash("房间不存在或链接错误！", 'danger')
        return redirect(url_for('create_room'))

    # 获取当前已登记的玩家名单
    cursor.execute("SELECT name, secret_code FROM participants WHERE room_id = %s", (room_id,))
    participants_rows = cursor.fetchall()
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
            existing_participant = any(p['name'] == name for p in participants_rows)

            if not name or not secret_code:
                flash("名字和查询码不能为空。", 'danger')
            elif existing_participant:
                flash(f"名字 '{name}' 已存在。您已登记成功，请勿重复操作。", 'warning')
            elif room['status'] != 'OPEN':
                flash("房间已完成匹配，无法再加入。", 'danger')
            elif current_count >= room['target_count']:
                flash("房间人数已满，无法再加入。", 'danger')
            else:
                # 名字未存在，执行插入操作
                try:
                    cursor.execute("INSERT INTO participants (room_id, name, secret_code) VALUES (%s, %s, %s)",
                                   (room_id, name, secret_code))
                    db.commit()
                    flash(f"登记成功！您是第 {current_count + 1} 位玩家。", 'success')
                    
                    new_count = current_count + 1 
                    
                    # **2. 自动触发匹配**
                    if new_count == room['target_count']:
                        # 重新查询最新玩家列表 (包括刚刚插入的)
                        cursor.execute("SELECT name FROM participants WHERE room_id = %s", (room_id,))
                        new_participants = cursor.fetchall() 
                        matching_result = perform_matching(new_participants)
                        
                        if matching_result:
                            # 批量更新匹配结果
                            for guardian_name, target_name in matching_result.items():
                                cursor.execute("UPDATE participants SET target_name = %s WHERE room_id = %s AND name = %s",
                                           (target_name, room_id, guardian_name))
                            
                            # 更新房间状态
                            cursor.execute("UPDATE rooms SET status = %s WHERE room_id = %s", ('MATCHED', room_id))
                            db.commit()
                            flash("人数已满，匹配成功自动完成！🎉 您现在可以查询结果了。", 'info')
                        else:
                            flash("匹配失败，请联系发起人重试。", 'danger')
                    
                    # 成功登记后重定向，供前端设置 Local Storage 标记
                    cursor.close()
                    db.close()
                    return redirect(url_for('join_room', room_id=room_id, registered_name=name))

                except psycopg2.Error as e:
                    flash("数据库写入失败，请联系管理员。", 'danger')
                    db.rollback()
                    
        elif action == 'query':
            # **3. 结果查询操作**
            query_name = request.form['query_name'].strip()
            query_code = request.form['query_code'].strip()
            
            if room['status'] != 'MATCHED':
                flash("匹配尚未开始，请等待所有玩家登记完毕。", 'warning')
            elif not query_name or not query_code:
                flash("请完整输入名字和查询码。", 'danger')
            else:
                # 核心：根据 Room ID, 名字和查询码进行校验
                cursor.execute("SELECT target_name FROM participants WHERE room_id = %s AND name = %s AND secret_code = %s",
                                (room_id, query_name, query_code))
                result = cursor.fetchone()
                
                if result and result['target_name']:
                    flash(f"🎉 您的守护对象是：**{result['target_name']}**", 'success')
                else:
                    flash("查询失败：名字或查询码不正确。", 'danger')
            
            cursor.close()
            db.close()
            return redirect(url_for('join_room', room_id=room_id))
    
    # GET 请求结束清理
    cursor.close()
    db.close()
    
    # --- GET: 页面渲染 ---
    return render_template('page2_join.html', 
                           room=room, 
                           participants_names=participants_names, 
                           current_count=len(participants_names))

# 确保数据库在应用被 Gunicorn 加载时初始化
if DATABASE_URL:
    with app.app_context():
        print("Initializing PostgreSQL database...")
        init_db()
else:
    print("Warning: DATABASE_URL not set. Running in local test mode (expect database errors in Render).")

# --- 应用启动 (本地测试) ---
if __name__ == '__main__':
    # 仅在本地开发环境中运行 Flask 自带服务器
    app.run(debug=True)
