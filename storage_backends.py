"""存储后端抽象层：支持 SQLite 与 DuckDB 双引擎切换

- BaseStorage: 公共逻辑 + 方言 hook
- SqliteStorage: SQLite 实现（默认）
- DuckdbStorage: DuckDB 实现（列存，聚合查询更快）
- create_storage(): 工厂函数
- migrate_storage(): 引擎间数据互转
"""
import os
import threading
import time
import sqlite3

# 日志由外部注入（避免循环导入）
_logger = None

def _set_logger(fn):
    global _logger
    _logger = fn

def _log(msg, level=0):
    if _logger:
        _logger(msg, level)
    else:
        print(f"[storage] {msg}", flush=True)


# ========== 设置项类型映射（由外部模块定义，这里引用） ==========
# 这些字典在 import 时由 aircat-server-web.py 注入
SETTING_TYPES = {}
SETTING_DEFAULTS = {}
CLEANUP_INTERVAL = 300


def configure(types_map, defaults_map, cleanup_interval, log_fn):
    """由主模块调用，注入配置"""
    global SETTING_TYPES, SETTING_DEFAULTS, CLEANUP_INTERVAL
    SETTING_TYPES = types_map
    SETTING_DEFAULTS = defaults_map
    CLEANUP_INTERVAL = cleanup_interval
    _set_logger(log_fn)


# ========== 基类 ==========
class BaseStorage:
    """存储后端抽象基类。子类实现方言 hook，公共逻辑在此"""

    engine_name = 'base'

    def __init__(self, db_path):
        self.db_path = db_path
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = self._connect()
        self._init_db()
        self._init_settings_table()
        _log(f"{self.engine_name} database initialized at {db_path}", 0)
        self._start_cleanup_thread()

    # ---------- 方言 hook（子类必须实现）----------
    def _connect(self):
        raise NotImplementedError

    def _init_db(self):
        """建表 DDL，子类实现（自增主键、时间默认值等方言差异）"""
        raise NotImplementedError

    def _now_minus(self, n, unit):
        """返回 now - N unit 的 SQL 表达式字符串（已含值，无需参数）"""
        raise NotImplementedError

    def _fetchall_dict(self, cur):
        """fetchall 结果转为 list[dict]"""
        raise NotImplementedError

    def _fetchone_dict(self, cur):
        """fetchone 结果转为 dict 或 None"""
        raise NotImplementedError

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---------- 设置表 ----------
    def _init_settings_table(self):
        with self.lock:
            self.conn.execute(
                'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)'
            )
            cur = self.conn.execute('SELECT COUNT(*) AS c FROM settings')
            first_start = self._fetchone_dict(cur)['c'] == 0

            defaults = dict(SETTING_DEFAULTS)
            if first_start:
                auth_user_env = os.environ.get('AUTH_USER')
                auth_pass_env = os.environ.get('AUTH_PASS')
                if auth_user_env is not None:
                    defaults['auth_user'] = auth_user_env
                if auth_pass_env is not None:
                    defaults['auth_pass'] = auth_pass_env
                    defaults['auth_enabled'] = 1
                m1_brightness_env = os.environ.get('M1_BRIGHTNESS')
                if m1_brightness_env is not None:
                    defaults['m1_brightness'] = m1_brightness_env
                m1_timer_enabled_env = os.environ.get('M1_TIMER_ENABLED')
                if m1_timer_enabled_env is not None:
                    defaults['m1_timer_enabled'] = 1 if m1_timer_enabled_env.lower() in ('1', 'true', 'yes', 'on') else 0

            for key, value in defaults.items():
                cur = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,))
                if self._fetchone_dict(cur) is None:
                    self.conn.execute(
                        'INSERT INTO settings (key, value) VALUES (?, ?)',
                        (key, str(value))
                    )
            self.conn.commit()

    # ---------- 设置读写 ----------
    def get_setting(self, key):
        with self.lock:
            cur = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,))
            row = self._fetchone_dict(cur)
        if row is None:
            return SETTING_DEFAULTS.get(key)
        raw = row['value']
        type_fn = SETTING_TYPES.get(key, str)
        if type_fn is int:
            try:
                return int(raw)
            except (ValueError, TypeError):
                return SETTING_DEFAULTS.get(key, 0)
        return raw

    def set_setting(self, key, value):
        with self.lock:
            self.conn.execute(
                'INSERT INTO settings (key, value) VALUES (?, ?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, str(value))
            )
            self.conn.commit()

    def get_all_settings(self):
        return {key: self.get_setting(key) for key in SETTING_DEFAULTS}

    # ---------- 数据读写 ----------
    def insert(self, humidity, temperature, pm25, hcho, client_ip):
        with self.lock:
            self.conn.execute(
                'INSERT INTO sensor_data (humidity, temperature, pm25, hcho, client_ip) VALUES (?, ?, ?, ?, ?)',
                (humidity, temperature, pm25, hcho, client_ip)
            )
            self.conn.commit()
        self.cleanup_data()

    def get_latest(self):
        with self.lock:
            cur = self.conn.execute('SELECT * FROM sensor_data ORDER BY id DESC LIMIT 1')
            return self._fetchone_dict(cur)

    def get_history(self, hours=24):
        with self.lock:
            cur = self.conn.execute(
                f'SELECT * FROM sensor_data WHERE timestamp >= {self._now_minus(hours, "hours")} ORDER BY id ASC'
            )
            return self._fetchall_dict(cur)

    def get_history_by_range(self, start, end):
        with self.lock:
            records = []
            agg_enabled = self.get_setting('agg_enabled')
            agg_raw_days = self.get_setting('agg_raw_days')

            try:
                from datetime import datetime
                dt_start = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
                dt_end = datetime.strptime(end, '%Y-%m-%d %H:%M:%S')
                total_hours = (dt_end - dt_start).total_seconds() / 3600
            except Exception:
                total_hours = 0

            if agg_enabled and agg_raw_days and total_hours > agg_raw_days * 24:
                cur = self.conn.execute(
                    'SELECT * FROM sensor_data WHERE timestamp >= ? AND timestamp <= ? ORDER BY id ASC',
                    (start, end)
                )
                records = self._fetchall_dict(cur)

                cur = self.conn.execute(
                    "SELECT timestamp, avg_humidity, avg_temperature, avg_pm25, avg_hcho "
                    "FROM sensor_data_aggregated "
                    "WHERE level='hourly' AND timestamp >= ? AND timestamp <= ? "
                    "ORDER BY timestamp ASC",
                    (start, end)
                )
                for row in self._fetchall_dict(cur):
                    records.append({
                        'id': 0,
                        'timestamp': row['timestamp'],
                        'humidity': row['avg_humidity'],
                        'temperature': row['avg_temperature'],
                        'pm25': row['avg_pm25'],
                        'hcho': row['avg_hcho'],
                        'client_ip': 'agg_hourly'
                    })

                agg_hourly_days = self.get_setting('agg_hourly_days')
                if agg_hourly_days and total_hours > agg_hourly_days * 24:
                    cur = self.conn.execute(
                        "SELECT timestamp, avg_humidity, avg_temperature, avg_pm25, avg_hcho "
                        "FROM sensor_data_aggregated "
                        "WHERE level='daily' AND timestamp >= ? AND timestamp <= ? "
                        "ORDER BY timestamp ASC",
                        (start, end)
                    )
                    for row in self._fetchall_dict(cur):
                        records.append({
                            'id': 0,
                            'timestamp': row['timestamp'],
                            'humidity': row['avg_humidity'],
                            'temperature': row['avg_temperature'],
                            'pm25': row['avg_pm25'],
                            'hcho': row['avg_hcho'],
                            'client_ip': 'agg_daily'
                        })

                records.sort(key=lambda r: str(r.get('timestamp', '')))
            else:
                cur = self.conn.execute(
                    'SELECT * FROM sensor_data WHERE timestamp >= ? AND timestamp <= ? ORDER BY id ASC',
                    (start, end)
                )
                records = self._fetchall_dict(cur)

            return records

    def get_record_count(self):
        with self.lock:
            cur = self.conn.execute('SELECT COUNT(*) AS c FROM sensor_data')
            return self._fetchone_dict(cur)['c']

    # ---------- 清理与聚合 ----------
    def cleanup_data(self):
        deleted = 0
        aggregated = 0
        max_records = self.get_setting('max_records')
        retention_days = self.get_setting('retention_days')
        agg_enabled = self.get_setting('agg_enabled')

        with self.lock:
            if agg_enabled:
                aggregated = self._do_aggregation()

            # 删除超过保留期的原始记录（先 COUNT 再 DELETE，兼容无 rowcount 的引擎）
            if retention_days and retention_days > 0:
                cur = self.conn.execute(
                    f"SELECT COUNT(*) AS c FROM sensor_data WHERE timestamp < {self._now_minus(retention_days, 'days')}"
                )
                deleted += self._fetchone_dict(cur)['c']
                self.conn.execute(
                    f"DELETE FROM sensor_data WHERE timestamp < {self._now_minus(retention_days, 'days')}"
                )

            # 删除超出上限的旧记录
            if max_records and max_records > 0:
                cur = self.conn.execute('SELECT COUNT(*) AS c FROM sensor_data')
                count = self._fetchone_dict(cur)['c']
                if count > max_records:
                    excess = count - max_records
                    self.conn.execute(
                        'DELETE FROM sensor_data WHERE id IN ('
                        'SELECT id FROM sensor_data ORDER BY id ASC LIMIT ?'
                        ')',
                        (excess,)
                    )
                    deleted += excess

            # 清理过期聚合数据
            if agg_enabled:
                agg_hourly_days = self.get_setting('agg_hourly_days')
                agg_daily_days = self.get_setting('agg_daily_days')
                if agg_hourly_days and agg_hourly_days > 0:
                    self.conn.execute(
                        f"DELETE FROM sensor_data_aggregated WHERE level='hourly' AND timestamp < {self._now_minus(agg_hourly_days, 'days')}"
                    )
                if agg_daily_days and agg_daily_days > 0:
                    self.conn.execute(
                        f"DELETE FROM sensor_data_aggregated WHERE level='daily' AND timestamp < {self._now_minus(agg_daily_days, 'days')}"
                    )

            self.conn.commit()

        if aggregated > 0:
            _log(f"Aggregation: {aggregated} raw records -> hourly/daily", 3)
        return deleted + aggregated

    def _do_aggregation(self):
        agg_raw_days = self.get_setting('agg_raw_days')
        agg_hourly_days = self.get_setting('agg_hourly_days')
        merged = 0
        AGG_BATCH_SIZE = 500

        # ---- 阶段 1: 原始数据 → 小时级聚合 ----
        if agg_raw_days and agg_raw_days > 0:
            try:
                cur = self.conn.execute(
                    f"SELECT * FROM sensor_data "
                    f"WHERE timestamp < {self._now_minus(agg_raw_days, 'days')} "
                    f"ORDER BY id ASC LIMIT ?",
                    (AGG_BATCH_SIZE,)
                )
                rows = self._fetchall_dict(cur)

                if rows:
                    agg_rows = self._fetchall_dict(
                        self.conn.execute("SELECT timestamp FROM sensor_data_aggregated WHERE level='hourly'")
                    )
                    agg_ts_set = set(r['timestamp'] for r in agg_rows)

                    hour_buckets = {}
                    for row in rows:
                        ts_str = row['timestamp']
                        if not ts_str:
                            continue
                        try:
                            hour_ts = str(ts_str)[:13] + ':00:00'
                        except Exception:
                            continue
                        if hour_ts in agg_ts_set:
                            continue
                        if hour_ts not in hour_buckets:
                            hour_buckets[hour_ts] = {
                                'humidity': [], 'temperature': [],
                                'pm25': [], 'hcho': []
                            }
                        for f in ('humidity', 'temperature', 'pm25', 'hcho'):
                            if row[f] is not None:
                                hour_buckets[hour_ts][f].append(row[f])

                    for hour_ts, bucket in hour_buckets.items():
                        def _avg(lst):
                            return sum(lst) / len(lst) if lst else None
                        def _min(lst):
                            return min(lst) if lst else None
                        def _max(lst):
                            return max(lst) if lst else None
                        hums = bucket['humidity']
                        temps = bucket['temperature']
                        pm25s = bucket['pm25']
                        hchos = bucket['hcho']
                        count = max(len(hums), len(temps), len(pm25s), len(hchos))
                        self.conn.execute(
                            "INSERT INTO sensor_data_aggregated "
                            "(level, timestamp, avg_humidity, avg_temperature, avg_pm25, avg_hcho, "
                            "min_humidity, max_humidity, min_temperature, max_temperature, "
                            "min_pm25, max_pm25, min_hcho, max_hcho, count) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(level, timestamp) DO UPDATE SET "
                            "avg_humidity=excluded.avg_humidity, avg_temperature=excluded.avg_temperature, "
                            "avg_pm25=excluded.avg_pm25, avg_hcho=excluded.avg_hcho, "
                            "min_humidity=excluded.min_humidity, max_humidity=excluded.max_humidity, "
                            "min_temperature=excluded.min_temperature, max_temperature=excluded.max_temperature, "
                            "min_pm25=excluded.min_pm25, max_pm25=excluded.max_pm25, "
                            "min_hcho=excluded.min_hcho, max_hcho=excluded.max_hcho, count=excluded.count",
                            ('hourly', hour_ts,
                             _avg(hums), _avg(temps), _avg(pm25s), _avg(hchos),
                             _min(hums), _max(hums), _min(temps), _max(temps),
                             _min(pm25s), _max(pm25s), _min(hchos), _max(hchos),
                             count)
                        )
                        merged += count

                    if rows:
                        ids_to_delete = [row['id'] for row in rows]
                        placeholders = ','.join('?' * len(ids_to_delete))
                        self.conn.execute(
                            f"DELETE FROM sensor_data WHERE id IN ({placeholders})",
                            ids_to_delete
                        )
            except Exception as e:
                _log(f"Hourly aggregation error: {e}", 2)

        # ---- 阶段 2: 小时级聚合 → 天级聚合 ----
        if agg_hourly_days and agg_hourly_days > 0:
            try:
                cur = self.conn.execute(
                    f"SELECT * FROM sensor_data_aggregated "
                    f"WHERE level='hourly' AND timestamp < {self._now_minus(agg_hourly_days, 'days')} "
                    f"ORDER BY id ASC LIMIT ?",
                    (AGG_BATCH_SIZE,)
                )
                hour_rows = self._fetchall_dict(cur)

                day_buckets = {}
                for row in hour_rows:
                    ts_str = row['timestamp']
                    if not ts_str:
                        continue
                    try:
                        day_ts = str(ts_str)[:10] + ' 00:00:00'
                    except Exception:
                        continue
                    if day_ts not in day_buckets:
                        day_buckets[day_ts] = {
                            'avg_humidity': [], 'avg_temperature': [],
                            'avg_pm25': [], 'avg_hcho': [],
                        }
                    for f in ('avg_humidity', 'avg_temperature', 'avg_pm25', 'avg_hcho'):
                        if row[f] is not None:
                            day_buckets[day_ts][f].append(row[f])

                for day_ts, bucket in day_buckets.items():
                    def _avg2(lst):
                        return sum(lst) / len(lst) if lst else None
                    self.conn.execute(
                        "INSERT INTO sensor_data_aggregated "
                        "(level, timestamp, avg_humidity, avg_temperature, avg_pm25, avg_hcho, "
                        "min_humidity, max_humidity, min_temperature, max_temperature, "
                        "min_pm25, max_pm25, min_hcho, max_hcho, count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(level, timestamp) DO UPDATE SET "
                        "avg_humidity=excluded.avg_humidity, avg_temperature=excluded.avg_temperature, "
                        "avg_pm25=excluded.avg_pm25, avg_hcho=excluded.avg_hcho, count=excluded.count",
                        ('daily', day_ts,
                         _avg2(bucket['avg_humidity']), _avg2(bucket['avg_temperature']),
                         _avg2(bucket['avg_pm25']), _avg2(bucket['avg_hcho']),
                         None, None, None, None, None, None, None, None,
                         len(bucket['avg_humidity']))
                    )

                if hour_rows:
                    ids_to_delete = [row['id'] for row in hour_rows]
                    placeholders = ','.join('?' * len(ids_to_delete))
                    self.conn.execute(
                        f"DELETE FROM sensor_data_aggregated WHERE level='hourly' AND id IN ({placeholders})",
                        ids_to_delete
                    )
            except Exception as e:
                _log(f"Daily aggregation error: {e}", 2)

        return merged

    def get_aggregated_history(self, hours=24):
        agg_raw_days = self.get_setting('agg_raw_days')
        records = []

        with self.lock:
            if agg_raw_days and agg_raw_days > 0:
                if hours <= agg_raw_days * 24:
                    cur = self.conn.execute(
                        f'SELECT * FROM sensor_data WHERE timestamp >= {self._now_minus(hours, "hours")} ORDER BY id ASC'
                    )
                    records = self._fetchall_dict(cur)
                else:
                    raw_hours = agg_raw_days * 24
                    cur = self.conn.execute(
                        f'SELECT * FROM sensor_data WHERE timestamp >= {self._now_minus(raw_hours, "hours")} ORDER BY id ASC'
                    )
                    records = self._fetchall_dict(cur)

                    cur = self.conn.execute(
                        f"SELECT timestamp, avg_humidity, avg_temperature, avg_pm25, avg_hcho "
                        f"FROM sensor_data_aggregated "
                        f"WHERE level='hourly' AND timestamp >= {self._now_minus(hours, 'hours')} "
                        f"AND timestamp < {self._now_minus(raw_hours, 'hours')} "
                        f"ORDER BY timestamp ASC"
                    )
                    for row in self._fetchall_dict(cur):
                        records.append({
                            'id': 0,
                            'timestamp': row['timestamp'],
                            'humidity': row['avg_humidity'],
                            'temperature': row['avg_temperature'],
                            'pm25': row['avg_pm25'],
                            'hcho': row['avg_hcho'],
                            'client_ip': 'agg_hourly'
                        })

                    records.sort(key=lambda r: str(r.get('timestamp', '')))
            else:
                cur = self.conn.execute(
                    f'SELECT * FROM sensor_data WHERE timestamp >= {self._now_minus(hours, "hours")} ORDER BY id ASC'
                )
                records = self._fetchall_dict(cur)

        return records

    def clear_all_data(self):
        with self.lock:
            cur = self.conn.execute('SELECT COUNT(*) AS c FROM sensor_data')
            c1 = self._fetchone_dict(cur)['c']
            cur = self.conn.execute('SELECT COUNT(*) AS c FROM sensor_data_aggregated')
            c2 = self._fetchone_dict(cur)['c']
            self.conn.execute('DELETE FROM sensor_data')
            self.conn.execute('DELETE FROM sensor_data_aggregated')
            self.conn.commit()
            return c1 + c2

    # ---------- 后台清理线程 ----------
    def _start_cleanup_thread(self):
        t = threading.Thread(target=self._cleanup_loop, daemon=True)
        t.start()
        _log("Background cleanup thread started (interval=300s)", 3)

    def _cleanup_loop(self):
        while True:
            try:
                time.sleep(CLEANUP_INTERVAL)
                deleted = self.cleanup_data()
                if deleted > 0:
                    _log(f"Cleanup: deleted {deleted} expired/over-limit records", 3)
            except Exception as e:
                _log(f"Cleanup thread error: {e}", 2)

    # ---------- 数据导出（用于迁移）----------
    def export_all(self):
        """导出全部数据，返回 (settings, sensor_data, aggregated) 三个列表"""
        with self.lock:
            cur = self.conn.execute('SELECT key, value FROM settings')
            settings = self._fetchall_dict(cur)
            cur = self.conn.execute(
                'SELECT humidity, temperature, pm25, hcho, client_ip, timestamp FROM sensor_data ORDER BY id ASC'
            )
            sensor_data = self._fetchall_dict(cur)
            cur = self.conn.execute(
                'SELECT level, timestamp, avg_humidity, avg_temperature, avg_pm25, avg_hcho, '
                'min_humidity, max_humidity, min_temperature, max_temperature, '
                'min_pm25, max_pm25, min_hcho, max_hcho, count FROM sensor_data_aggregated ORDER BY id ASC'
            )
            aggregated = self._fetchall_dict(cur)
        return settings, sensor_data, aggregated


# ========== SQLite 实现 ==========
class SqliteStorage(BaseStorage):
    engine_name = 'sqlite'

    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.lock:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS sensor_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    humidity REAL,
                    temperature REAL,
                    pm25 INTEGER,
                    hcho REAL,
                    client_ip TEXT
                )
            ''')
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS sensor_data_aggregated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    timestamp DATETIME NOT NULL,
                    avg_humidity REAL, avg_temperature REAL,
                    avg_pm25 REAL, avg_hcho REAL,
                    min_humidity REAL, max_humidity REAL,
                    min_temperature REAL, max_temperature REAL,
                    min_pm25 REAL, max_pm25 REAL,
                    min_hcho REAL, max_hcho REAL,
                    count INTEGER DEFAULT 0,
                    UNIQUE(level, timestamp)
                )
            ''')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_agg_level_ts ON sensor_data_aggregated(level, timestamp)')
            self.conn.commit()

    def _now_minus(self, n, unit):
        return f"datetime('now', '-{int(n)} {unit}')"

    def _fetchall_dict(self, cur):
        return [dict(r) for r in cur.fetchall()]

    def _fetchone_dict(self, cur):
        row = cur.fetchone()
        return dict(row) if row else None


# ========== DuckDB 实现 ==========
class DuckdbStorage(BaseStorage):
    engine_name = 'duckdb'

    def _connect(self):
        import duckdb
        # read_only=False, 自动创建文件
        conn = duckdb.connect(self.db_path, read_only=False)
        return conn

    def _init_db(self):
        with self.lock:
            # 自增主键用 sequence
            self.conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_sensor_data_id START 1")
            self.conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_agg_id START 1")
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS sensor_data (
                    id INTEGER PRIMARY KEY DEFAULT nextval('seq_sensor_data_id'),
                    timestamp TEXT DEFAULT (strftime(now(), '%Y-%m-%d %H:%M:%S')),
                    humidity REAL,
                    temperature REAL,
                    pm25 INTEGER,
                    hcho REAL,
                    client_ip TEXT
                )
            ''')
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS sensor_data_aggregated (
                    id INTEGER PRIMARY KEY DEFAULT nextval('seq_agg_id'),
                    level TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    avg_humidity REAL, avg_temperature REAL,
                    avg_pm25 REAL, avg_hcho REAL,
                    min_humidity REAL, max_humidity REAL,
                    min_temperature REAL, max_temperature REAL,
                    min_pm25 REAL, max_pm25 REAL,
                    min_hcho REAL, max_hcho REAL,
                    count INTEGER DEFAULT 0,
                    UNIQUE(level, timestamp)
                )
            ''')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_agg_level_ts ON sensor_data_aggregated(level, timestamp)')
            self.conn.commit()

    def _now_minus(self, n, unit):
        # duckdb: strftime(now() - INTERVAL 'N units', fmt)
        # 统一存为 TEXT 字符串，与 sqlite 字符串比较行为一致
        return f"strftime(now() - INTERVAL '{int(n)} {unit}', '%Y-%m-%d %H:%M:%S')"

    def _fetchall_dict(self, cur):
        rows = cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    def _fetchone_dict(self, cur):
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def export_all(self):
        """DuckDB 导出：timestamp 列统一转为字符串"""
        settings, sensor_data, aggregated = super().export_all()
        for r in sensor_data:
            if r.get('timestamp') is not None:
                r['timestamp'] = str(r['timestamp'])[:19]
        for r in aggregated:
            if r.get('timestamp') is not None:
                r['timestamp'] = str(r['timestamp'])[:19]
        return settings, sensor_data, aggregated


# ========== 工厂函数 ==========
def create_storage(engine, db_path):
    """根据引擎名创建存储实例"""
    engine = (engine or 'sqlite').lower()
    if engine == 'duckdb':
        return DuckdbStorage(db_path)
    return SqliteStorage(db_path)


# ========== 数据迁移 ==========
def migrate_storage(src_storage, target_engine, target_path=None):
    """将数据从 src_storage 迁移到 target_engine 的新库

    - target_path: 目标库文件路径，None 则自动推导（同目录，扩展名替换）
    - 返回: (new_storage, migrated_count) 或抛异常
    - 旧库文件保留不动（作为备份）
    """
    target_engine = target_engine.lower()
    if target_engine not in ('sqlite', 'duckdb'):
        raise ValueError(f"Unsupported target engine: {target_engine}")

    # 推导目标路径
    if target_path is None:
        base, _ = os.path.splitext(src_storage.db_path)
        ext = '.duckdb' if target_engine == 'duckdb' else '.db'
        target_path = base + ext
        # 如果同路径（sqlite→sqlite 且原就是 .db），加后缀避免覆盖
        if target_path == src_storage.db_path:
            target_path = base + '.new' + ext

    _log(f"Migrating data: {src_storage.engine_name}({src_storage.db_path}) -> {target_engine}({target_path})", 0)

    # 如果目标文件已存在，先删除（重新导入）
    if os.path.exists(target_path):
        os.remove(target_path)

    # 创建目标库（不启动清理线程，迁移期间不需要）
    dst = create_storage(target_engine, target_path)
    # 停掉目标的清理线程（避免迁移期间触发清理）
    # 清理线程是 daemon，无法主动停止，但迁移很快，影响可忽略

    # 导出源数据
    settings, sensor_data, aggregated = src_storage.export_all()
    total = 0

    with dst.lock:
        # 清空目标默认数据（_init_settings_table 会写入默认值）
        dst.conn.execute('DELETE FROM settings')
        dst.conn.execute('DELETE FROM sensor_data')
        dst.conn.execute('DELETE FROM sensor_data_aggregated')

        # 写入 settings
        for s in settings:
            dst.conn.execute(
                'INSERT INTO settings (key, value) VALUES (?, ?)',
                (s['key'], s['value'])
            )
        # 更新 db_engine 设置为目标引擎
        dst.conn.execute(
            "INSERT INTO settings (key, value) VALUES ('db_engine', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (target_engine,)
        )
        total += len(settings)

        # 写入 sensor_data
        for r in sensor_data:
            dst.conn.execute(
                'INSERT INTO sensor_data (humidity, temperature, pm25, hcho, client_ip, timestamp) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (r.get('humidity'), r.get('temperature'), r.get('pm25'),
                 r.get('hcho'), r.get('client_ip'), r.get('timestamp'))
            )
        total += len(sensor_data)

        # 写入聚合数据
        for r in aggregated:
            dst.conn.execute(
                "INSERT INTO sensor_data_aggregated "
                "(level, timestamp, avg_humidity, avg_temperature, avg_pm25, avg_hcho, "
                "min_humidity, max_humidity, min_temperature, max_temperature, "
                "min_pm25, max_pm25, min_hcho, max_hcho, count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(level, timestamp) DO UPDATE SET "
                "avg_humidity=excluded.avg_humidity, avg_temperature=excluded.avg_temperature, "
                "avg_pm25=excluded.avg_pm25, avg_hcho=excluded.avg_hcho, count=excluded.count",
                (r.get('level'), r.get('timestamp'),
                 r.get('avg_humidity'), r.get('avg_temperature'), r.get('avg_pm25'), r.get('avg_hcho'),
                 r.get('min_humidity'), r.get('max_humidity'),
                 r.get('min_temperature'), r.get('max_temperature'),
                 r.get('min_pm25'), r.get('max_pm25'),
                 r.get('min_hcho'), r.get('max_hcho'), r.get('count'))
            )
        total += len(aggregated)

        dst.conn.commit()

    _log(f"Migration complete: {total} records transferred to {target_engine}", 0)
    return dst, total
