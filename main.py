"""
串口交互监听工具
- 后台线程持续监听串口并打印收到的数据
- 主线程接收用户输入，支持单次发送和两种循环发送模式

循环模式说明：
  模式A（按时间间隔）: loop on  1.0
  模式B（收到返回值后再发）: loop rx

循环配置流程：
  1. 输入 loop on [间隔] 或 loop rx
  2. 输入要循环发送的命令（HEX 或文本）
  3. 输入目标循环次数（0 = 无限循环）
  4. 输入 loop off 或 Ctrl+C 可随时手动停止

返回值解析（正则）：
  格式示例: 101,101,1PC013012D,01,4A,4B,00,00,-84,OK
  - 末尾是否为 OK
  - 倒数第二个字段为信号强度（如 -84）

终止统计：
  - 总循环次数、总运行时间
  - 每次命令发送→收到返回值 的时间间隔 Avg/Min/Max
  - 信号强度 Avg/Min/Max

其他命令：
  loop off   → 手动停止循环
  quit/exit  → 退出程序
"""

import serial
import threading
import time
import sys
import re
import datetime
import matplotlib.pyplot as plt
import matplotlib
# 使用支持中文的字体，避免图表中文乱码
matplotlib.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
matplotlib.rcParams['axes.unicode_minus'] = False

# ──────────────────────────────────────────────
# 默认配置（运行时可修改）
# ──────────────────────────────────────────────
DEFAULT_PORT     = 'COM6'
DEFAULT_BAUDRATE = 9600
TIMEOUT          = 10        # 串口读取超时（秒）
READ_SIZE        = 256       # 每次最多读取字节数

# ──────────────────────────────────────────────
# 正则：解析返回值
#   末尾字段: OK 或其他
#   倒数第二个字段: 信号强度（整数，可为负）
# ──────────────────────────────────────────────
# 匹配以逗号分隔、最后两个字段为 "<数字>,<状态>" 的格式
RESP_RE = re.compile(r'.*,(-?\d+),([^,\r\n]+)\s*$')


def parse_response(text: str):
    """
    解析 ASCII 响应行，返回 (is_ok, signal_strength)。
    is_ok: bool，末尾字段是否为 OK（大小写不敏感）
    signal_strength: int 或 None（解析失败时）
    """
    m = RESP_RE.search(text.strip())
    if m:
        signal = int(m.group(1))
        status = m.group(2).strip().upper()
        return status == 'OK', signal
    return None, None


# ──────────────────────────────────────────────
# 全局状态
# ──────────────────────────────────────────────
loop_running  = False          # 循环发送总开关
loop_cmd      = b''            # 循环发送的命令字节
loop_interval = 1.0            # 模式A 的间隔时间（秒）
loop_mode     = 'time'         # 'time' = 按时间间隔，'rx' = 收到返回值后发
loop_target   = 0              # 目标次数，0 = 无限

stop_event    = threading.Event()   # 通知所有线程退出

# 统计数据（由循环线程写入，主线程读取打印）
stats_lock       = threading.Lock()
stat_send_count  = 0            # 实际发送次数
stat_ok_count    = 0            # 返回 OK 的次数
stat_rtt_list    = []           # 每次 RTT（往返时间，秒）
stat_signal_list = []           # 每次信号强度
stat_start_time  = 0.0          # 循环开始时间戳

# RX 模式专用：reader 线程收到数据后通知 sender 线程
rx_event = threading.Event()    # 收到数据时 set
rx_data_buf = []                # 收到的最新数据文本（list 保一个元素，线程安全用锁）
rx_buf_lock = threading.Lock()


def parse_command(text: str) -> bytes:
    """
    将用户输入字符串解析为字节。
    先尝试空格/逗号分隔的 HEX；失败则当 UTF-8 字符串。
    """
    cleaned = text.replace(',', ' ').replace('0x', '').replace('0X', '')
    tokens  = cleaned.split()
    try:
        return bytes(int(t, 16) for t in tokens)
    except ValueError:
        return text.encode('utf-8')


def print_stats():
    """打印本次循环的汇总统计，并生成折线图。"""
    with stats_lock:
        count   = stat_send_count
        ok      = stat_ok_count
        rtt     = list(stat_rtt_list)
        signals = list(stat_signal_list)
        elapsed = time.time() - stat_start_time if stat_start_time else 0.0

    print('\n' + '=' * 60)
    print('  循环统计汇总')
    print('=' * 60)
    print(f'  总循环次数   : {count}')
    print(f'  OK 次数      : {ok}  ({ok/count*100:.1f}%)' if count else '  OK 次数      : 0')
    print(f'  总运行时间   : {elapsed:.2f} 秒')

    if rtt:
        print(f'  响应时间(RTT)')
        print(f'    Avg : {sum(rtt)/len(rtt)*1000:.1f} ms')
        print(f'    Min : {min(rtt)*1000:.1f} ms')
        print(f'    Max : {max(rtt)*1000:.1f} ms')
    else:
        print('  响应时间(RTT): 无数据')

    if signals:
        print(f'  信号强度')
        print(f'    Avg : {sum(signals)/len(signals):.1f}')
        print(f'    Min : {min(signals)}')
        print(f'    Max : {max(signals)}')
    else:
        print('  信号强度     : 无数据')

    print('=' * 60)

    # ── 生成折线图 ──
    if rtt or signals:
        plot_stats_chart(rtt, signals)


def plot_stats_chart(rtt_list: list, signal_list: list):
    """
    绘制响应时间和信号强度的折线图，保存并弹出显示。
    """
    has_rtt = bool(rtt_list)
    has_sig = bool(signal_list)

    if not has_rtt and not has_sig:
        return

    # 确定子图数量
    if has_rtt and has_sig:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))
    else:
        fig, ax = plt.subplots(figsize=(10, 5))
        if has_rtt:
            ax1 = ax
            ax2 = None
        else:
            ax2 = ax
            ax1 = None

    # ── 响应时间折线图 ──
    if has_rtt:
        x_rtt = list(range(1, len(rtt_list) + 1))
        y_rtt = [r * 1000 for r in rtt_list]  # 转换为毫秒

        ax1.plot(x_rtt, y_rtt, color='steelblue', linewidth=1.2,
                 marker='o', markersize=3, markerfacecolor='white')
        ax1.set_title('响应时间 (RTT)', fontsize=12)
        ax1.set_xlabel('发送次数', fontsize=10)
        ax1.set_ylabel('响应时间 (ms)', fontsize=10)
        ax1.grid(True, linestyle='--', alpha=0.5)

        # 标注平均线
        avg_rtt = sum(y_rtt) / len(y_rtt)
        ax1.axhline(y=avg_rtt, color='red', linestyle='--', linewidth=1, alpha=0.7,
                    label=f'平均: {avg_rtt:.1f} ms')
        ax1.legend(loc='upper right', fontsize=9)

    # ── 信号强度折线图 ──
    if has_sig:
        x_sig = list(range(1, len(signal_list) + 1))
        y_sig = signal_list

        target_ax = ax2 if has_rtt else ax2
        target_ax.plot(x_sig, y_sig, color='seagreen', linewidth=1.2,
                       marker='s', markersize=3, markerfacecolor='white')
        target_ax.set_title('信号强度', fontsize=12)
        target_ax.set_xlabel('发送次数', fontsize=10)
        target_ax.set_ylabel('信号强度', fontsize=10)
        target_ax.grid(True, linestyle='--', alpha=0.5)

        # 标注平均线
        avg_sig = sum(y_sig) / len(y_sig)
        target_ax.axhline(y=avg_sig, color='orange', linestyle='--', linewidth=1, alpha=0.7,
                          label=f'平均: {avg_sig:.1f}')
        target_ax.legend(loc='upper right', fontsize=9)

    plt.tight_layout()

    # ── 保存文件 ──
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'{timestamp}.png'
    fig.savefig(filename, dpi=150, bbox_inches='tight')
    print(f'[INFO] 图表已保存至：{filename}')

    # ── 弹出窗口 ──
    plt.show()


def reset_stats():
    """重置所有统计数据。"""
    global stat_send_count, stat_ok_count, stat_rtt_list, stat_signal_list, stat_start_time
    with stats_lock:
        stat_send_count  = 0
        stat_ok_count    = 0
        stat_rtt_list    = []
        stat_signal_list = []
        stat_start_time  = time.time()


# ──────────────────────────────────────────────
# 线程 1：持续监听串口并打印
# ──────────────────────────────────────────────
def reader_thread(ser: serial.Serial):
    """
    后台串口读取线程，收到数据立即打印，并通知 RX 模式的发送线程。

    读取策略：
    1. 检测到有数据后，等待一小段时间让完整帧到达
    2. 然后一次性读取缓冲区所有数据
    """
    while not stop_event.is_set():
        try:
            # 检查是否有数据
            if ser.in_waiting > 0:
                # 等待完整帧到达（根据波特率调整，9600bps 下 50ms 足够传约 60 字节）
                time.sleep(0.1)  # 100ms

                # 再次检查并读取所有数据
                if ser.in_waiting > 0:
                    data = ser.read(ser.in_waiting)
                else:
                    continue
            else:
                # 无数据时短暂休眠，避免 CPU 100%
                time.sleep(0.01)  # 10ms
                continue

            if data:
                hex_str  = data.hex().upper()
                try:
                    text = data.decode('utf-8', errors='replace')
                except Exception:
                    text = repr(data)

                print(f'\n  ← 收到 [{len(data)} 字节] HEX: {hex_str}  |  ASCII: {text.strip()!r}')

                # 解析 OK 和信号强度（只在循环运行期间统计）
                if loop_running:
                    is_ok, signal = parse_response(text)
                    if signal is not None:
                        with stats_lock:
                            global stat_ok_count, stat_signal_list
                            if is_ok:
                                stat_ok_count += 1
                            stat_signal_list.append(signal)
                        ok_tag = '✓ OK' if is_ok else '✗ NOT OK'
                        print(f'     解析: {ok_tag}  信号强度: {signal}')

                # 将原始文本写入缓冲区，通知 RX 模式发送线程
                with rx_buf_lock:
                    rx_data_buf.clear()
                    rx_data_buf.append(text)
                rx_event.set()

                print('> ', end='', flush=True)
        except serial.SerialException as e:
            if not stop_event.is_set():
                print(f'\n[WARN] 串口读取异常：{e}')
            break
        except Exception:
            break


# ──────────────────────────────────────────────
# 线程 2A：按时间间隔循环发送
# ──────────────────────────────────────────────
def loop_time_thread(ser: serial.Serial):
    """按固定时间间隔循环发送，直到 loop_running=False 或达到目标次数。"""
    global loop_running, stat_send_count
    while loop_running and not stop_event.is_set():
        send_time = time.time()
        try:
            ser.write(loop_cmd)
            with stats_lock:
                stat_send_count += 1
                current = stat_send_count
            print(f'\n  → [{current}] 循环发送(时间模式): {loop_cmd.hex().upper()}')
            print('> ', end='', flush=True)
        except serial.SerialException as e:
            print(f'\n[WARN] 循环发送失败：{e}')
            loop_running = False
            break

        # 等待收到响应以计算 RTT（最多等 TIMEOUT 秒）
        rx_event.clear()
        got_rx = rx_event.wait(timeout=TIMEOUT)
        if got_rx:
            rtt = time.time() - send_time
            with stats_lock:
                stat_rtt_list.append(rtt)

        # 检查是否达到目标次数
        with stats_lock:
            done = (loop_target > 0 and stat_send_count >= loop_target)
        if done:
            print(f'\n[INFO] 已达到目标循环次数 {loop_target}，循环结束')
            loop_running = False
            print_stats()
            break

        # 等剩余间隔时间
        used = time.time() - send_time
        remain = loop_interval - used
        elapsed = 0.0
        while elapsed < remain and loop_running and not stop_event.is_set():
            time.sleep(0.05)
            elapsed += 0.05


# ──────────────────────────────────────────────
# 线程 2B：收到返回值后再发（RX 触发模式）
# ──────────────────────────────────────────────
def loop_rx_thread(ser: serial.Serial):
    """
    收到串口返回值后立即发送下一次命令。
    第一次发送由本线程启动时主动触发。
    """
    global loop_running, stat_send_count

    while loop_running and not stop_event.is_set():
        send_time = time.time()
        try:
            ser.write(loop_cmd)
            with stats_lock:
                stat_send_count += 1
                current = stat_send_count
            print(f'\n  → [{current}] 循环发送(RX模式): {loop_cmd.hex().upper()}')
            print('> ', end='', flush=True)
        except serial.SerialException as e:
            print(f'\n[WARN] 循环发送失败：{e}')
            loop_running = False
            break

        # 等待串口返回数据，最多等 10 秒
        rx_event.clear()
        got_rx = rx_event.wait(timeout=10.0)
        if got_rx:
            rtt = time.time() - send_time
            with stats_lock:
                stat_rtt_list.append(rtt)
        else:
            print('\n  [WARN] 等待返回超时（10s），继续发送下一次')

        # 检查是否达到目标次数
        with stats_lock:
            done = (loop_target > 0 and stat_send_count >= loop_target)
        if done:
            print(f'\n[INFO] 已达到目标循环次数 {loop_target}，循环结束')
            loop_running = False
            print_stats()
            break


# ──────────────────────────────────────────────
# 主函数
# ──────────────────────────────────────────────
def main():
    global loop_running, loop_cmd, loop_interval, loop_mode, loop_target

    # ── 启动时询问串口配置 ──
    print('=' * 60)
    print('  串口交互监听工具 v2')
    print('=' * 60)

    # 询问串口号
    print(f'当前默认串口: {DEFAULT_PORT}')
    port_input = input('请输入串口号（直接回车使用默认值）: ').strip()
    port = port_input if port_input else DEFAULT_PORT

    # 询问波特率
    print(f'当前默认波特率: {DEFAULT_BAUDRATE}')
    baud_input = input('请输入波特率（直接回车使用默认值）: ').strip()
    if baud_input:
        try:
            baudrate = int(baud_input)
        except ValueError:
            print(f'[WARN] 波特率格式错误，使用默认值 {DEFAULT_BAUDRATE}')
            baudrate = DEFAULT_BAUDRATE
    else:
        baudrate = DEFAULT_BAUDRATE

    # 打开串口
    try:
        ser = serial.Serial(port, baudrate, timeout=TIMEOUT)
        print(f'\n[INFO] 已连接串口 {port}，波特率 {baudrate}')
    except serial.SerialException as e:
        print(f'[ERROR] 无法打开串口 {port}：{e}')
        sys.exit(1)

    # 启动后台监听线程
    t_reader = threading.Thread(target=reader_thread, args=(ser,), daemon=True)
    t_reader.start()

    print('=' * 60)
    print('  单次发送  : 直接输入 HEX 或文本')
    print('  循环(时间): loop on [间隔秒]  → 按固定间隔发送')
    print('  循环(RX)  : loop rx           → 收到返回值后发下一次')
    print('  停止循环  : loop off')
    print('  退出      : quit 或 Ctrl+C')
    print('=' * 60)

    loop_thread = None

    def stop_loop():
        """停止当前循环并打印统计。"""
        nonlocal loop_thread
        if loop_running:
            loop_running_was = True
        else:
            loop_running_was = False

        # 设置标志让循环线程退出
        globals()['loop_running'] = False  # 注意：此处直接操作全局
        # 等线程结束
        if loop_thread and loop_thread.is_alive():
            loop_thread.join(timeout=3)
        loop_thread = None

        if loop_running_was:
            print_stats()

    try:
        while True:
            print('> ', end='', flush=True)
            try:
                user_input = input().strip()
            except EOFError:
                break

            if not user_input:
                continue

            lower = user_input.lower()

            # ── 退出 ──
            if lower in ('quit', 'exit', 'q'):
                if loop_running:
                    loop_running = False
                    if loop_thread and loop_thread.is_alive():
                        loop_thread.join(timeout=3)
                    print_stats()
                print('[INFO] 正在退出……')
                break

            # ── 循环控制 ──
            if lower.startswith('loop'):
                parts = lower.split()

                # loop off
                if len(parts) >= 2 and parts[1] == 'off':
                    if loop_running:
                        loop_running = False
                        if loop_thread and loop_thread.is_alive():
                            loop_thread.join(timeout=3)
                        print('[INFO] 循环发送已关闭')
                        print_stats()
                    else:
                        print('[INFO] 当前没有运行中的循环')
                    continue

                # loop on [间隔] 或 loop rx
                if len(parts) >= 2 and parts[1] in ('on', 'rx'):
                    # 先停掉旧循环
                    if loop_running:
                        loop_running = False
                        if loop_thread and loop_thread.is_alive():
                            loop_thread.join(timeout=3)

                    # 确定模式和间隔
                    if parts[1] == 'rx':
                        loop_mode = 'rx'
                        print('[INFO] 模式：收到返回值后发送下一次（RX触发模式）')
                    else:
                        loop_mode = 'time'
                        if len(parts) >= 3:
                            try:
                                loop_interval = float(parts[2])
                            except ValueError:
                                print('[WARN] 间隔格式错误，使用默认 1.0 秒')
                                loop_interval = 1.0
                        else:
                            loop_interval = 1.0
                        print(f'[INFO] 模式：按时间间隔 {loop_interval}s 发送')

                    # 输入循环命令
                    print('[INFO] 请输入要循环发送的命令（HEX 或文本）：', end='', flush=True)
                    try:
                        cmd_input = input().strip()
                    except EOFError:
                        break
                    if not cmd_input:
                        print('[WARN] 命令为空，取消')
                        continue
                    loop_cmd = parse_command(cmd_input)

                    # 输入目标次数
                    print('[INFO] 请输入目标循环次数（0 = 无限循环，直到 loop off 或 Ctrl+C）：', end='', flush=True)
                    try:
                        cnt_input = input().strip()
                        loop_target = int(cnt_input) if cnt_input else 0
                    except (EOFError, ValueError):
                        loop_target = 0
                    if loop_target < 0:
                        loop_target = 0

                    target_desc = f'{loop_target} 次' if loop_target > 0 else '无限循环'
                    print(f'[INFO] 循环发送已开启，命令: {loop_cmd.hex().upper()}，目标: {target_desc}')

                    # 重置统计并启动线程
                    reset_stats()
                    loop_running = True
                    rx_event.clear()

                    if loop_mode == 'rx':
                        loop_thread = threading.Thread(
                            target=loop_rx_thread, args=(ser,), daemon=True)
                    else:
                        loop_thread = threading.Thread(
                            target=loop_time_thread, args=(ser,), daemon=True)
                    loop_thread.start()

                else:
                    print('[HINT] 用法:')
                    print('  loop on [间隔秒]  → 按时间间隔循环')
                    print('  loop rx           → 收到返回值后发送下一次')
                    print('  loop off          → 停止循环')
                continue

            # ── 单次发送 ──
            cmd = parse_command(user_input)
            try:
                ser.write(cmd)
                print(f'  → 已发送: {cmd.hex().upper()}  ({len(cmd)} 字节)')
            except serial.SerialException as e:
                print(f'[ERROR] 发送失败：{e}')

    except KeyboardInterrupt:
        print('\n[INFO] Ctrl+C，正在退出……')
        if loop_running:
            loop_running = False
            if loop_thread and loop_thread.is_alive():
                loop_thread.join(timeout=3)
            print_stats()
    finally:
        loop_running = False
        stop_event.set()
        if loop_thread and loop_thread.is_alive():
            loop_thread.join(timeout=2)
        ser.close()
        print('[INFO] 串口已关闭，程序结束')


if __name__ == '__main__':
    main()
