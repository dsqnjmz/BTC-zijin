import streamlit as st
import pandas as pd
import requests as std_requests
from curl_cffi import requests as curl_requests
from io import StringIO
import warnings
import time
import json
from decimal import Decimal
import plotly.graph_objects as go
from plotly.subplots import make_subplots

warnings.filterwarnings('ignore')
st.set_page_config(page_title="流动性边际变化雷达", page_icon="🦅", layout="wide")
# 👇 新增这段 CSS 魔法，强制放大 Tab 标签的字体
st.markdown(
    """
    <style>
    /* 调整第三层 Tab 标签页的字体大小和粗细 */
    button[data-baseweb="tab"] {
        font-size: 20px !important;
        font-weight: bold !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# ==========================================
# 🧠 核心数据引擎
# ==========================================
@st.cache_data(ttl=3600)
def load_and_process_data():
    # 1. 获取 BTC
    df_price = pd.DataFrame(std_requests.get("https://api.binance.com/api/v3/klines", params={"symbol": "BTCUSDT", "interval": "1d", "limit": 365}).json(), columns=['Open_time', 'Open', 'High', 'Low', 'Close', 'Volume', 'Close_time', 'Quote_asset_volume', 'Number_of_trades', 'Taker_buy_base_asset_volume', 'Taker_buy_quote_asset_volume', 'Ignore'])
    df_price['Date'] = pd.to_datetime(df_price['Open_time'], unit='ms').dt.strftime('%Y-%m-%d')
    df_price['BTC_Price'] = df_price['Close'].astype(float)
    df_price = df_price[['Date', 'BTC_Price']]

    # 2. 获取稳定币
    headers = {"User-Agent": "Mozilla/5.0"}
    df_usdt = pd.DataFrame(std_requests.get("https://api.coingecko.com/api/v3/coins/tether/market_chart?vs_currency=usd&days=365", headers=headers).json()['market_caps'], columns=['timestamp', 'USDT_Mcap'])
    df_usdt['Date'] = pd.to_datetime(df_usdt['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')
    time.sleep(1)
    df_usdc = pd.DataFrame(std_requests.get("https://api.coingecko.com/api/v3/coins/usd-coin/market_chart?vs_currency=usd&days=365", headers=headers).json()['market_caps'], columns=['timestamp', 'USDC_Mcap'])
    df_usdc['Date'] = pd.to_datetime(df_usdc['timestamp'], unit='ms').dt.strftime('%Y-%m-%d')
    df_stables = df_usdt[['Date', 'USDT_Mcap']].merge(df_usdc[['Date', 'USDC_Mcap']], on='Date', how='inner')
    df_stables['USDT_Mcap'] /= 1000000
    df_stables['USDC_Mcap'] /= 1000000
    df_stables['Total_Stable_Mcap'] = df_stables['USDT_Mcap'] + df_stables['USDC_Mcap']
    df_stables = df_stables.drop_duplicates(subset=['Date'], keep='last')

    # 3. 获取 ETF (仅交易日)
    headers_soso = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json;charset=UTF-8", "Origin": "https://sosovalue.com", "Referer": "https://sosovalue.com/", "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36", "user-device": "Chrome/145.0.0.0#Mac OS/10.15.7"}
    
    response = curl_requests.post("https://gw.sosovalue.com/finance/etf-statistics-do/findPage", headers=headers_soso, json={"pageNo": 1, "pageSize": 1000}, proxies=proxies, impersonate="chrome120", timeout=20)
    df_etf = pd.DataFrame(json.loads(response.text, parse_float=Decimal)["data"]["list"])[['dataDate', 'totalNetInflow']]
    df_etf.columns = ['Date', 'ETF_Net_Inflow']
    df_etf['Date'] = pd.to_datetime(df_etf['Date']).dt.strftime('%Y-%m-%d')
    df_etf['ETF_Net_Inflow'] = df_etf['ETF_Net_Inflow'].astype(float) / 1000000 
    df_etf = df_etf.sort_values('Date').reset_index(drop=True)
    df_etf['ETF_Cumsum'] = df_etf['ETF_Net_Inflow'].cumsum() # 用于画总量图

    # 📌 极其关键：在纯交易日历上算 ETF 斜率，解决周末断层
    df_etf['ETF_Velocity'] = df_etf['ETF_Net_Inflow'].ewm(span=3, adjust=False).mean() - df_etf['ETF_Net_Inflow'].ewm(span=7, adjust=False).mean()
    df_etf['ETF_Accel'] = df_etf['ETF_Velocity'] - df_etf['ETF_Velocity'].shift(1)

    # 4. 获取微策略
    tables = pd.read_html(StringIO(std_requests.get("https://bitbo.io/treasuries/microstrategy/", headers=headers).text))
    df_mstr = max(tables, key=len).iloc[:, [0, 1]].copy()
    df_mstr.columns = ['Date', 'MSTR_Bought']
    df_mstr['Date'] = pd.to_datetime(df_mstr['Date'], errors='coerce').dt.strftime('%Y-%m-%d')
    df_mstr = df_mstr.dropna(subset=['Date'])
    df_mstr['MSTR_Bought'] = pd.to_numeric(df_mstr['MSTR_Bought'].astype(str).str.replace(r'[^\d.]', '', regex=True), errors='coerce').fillna(0)
    
    # 合并宽表
    df_master = df_price.merge(df_stables, on='Date', how='left').merge(df_etf, on='Date', how='left').merge(df_mstr, on='Date', how='left').reset_index(drop=True)
    
    # 填充处理
    df_master[['USDT_Mcap', 'USDC_Mcap', 'Total_Stable_Mcap']] = df_master[['USDT_Mcap', 'USDC_Mcap', 'Total_Stable_Mcap']].ffill()
    df_master[['ETF_Velocity', 'ETF_Accel', 'ETF_Cumsum']] = df_master[['ETF_Velocity', 'ETF_Accel', 'ETF_Cumsum']].ffill()
    df_master['ETF_Net_Inflow'] = df_master['ETF_Net_Inflow'].fillna(0)
    df_master['MSTR_Bought'] = df_master['MSTR_Bought'].fillna(0)
    df_master['MSTR_Cumsum'] = df_master['MSTR_Bought'].cumsum()

    # 算增量
    df_master['Stable_Change'] = df_master['Total_Stable_Mcap'] - df_master['Total_Stable_Mcap'].shift(1)
    df_master['USDT_Change'] = df_master['USDT_Mcap'] - df_master['USDT_Mcap'].shift(1)
    df_master['USDC_Change'] = df_master['USDC_Mcap'] - df_master['USDC_Mcap'].shift(1)

    # 微策略隐形买盘平摊
    df_master['MSTR_Hidden_Buy_BTC'] = 0.0 
    last_buy_idx = 0 
    for i in range(len(df_master)):
        if df_master.loc[i, 'MSTR_Bought'] > 0:
            actual_days = i - last_buy_idx
            if actual_days > 0:
                twap_days = min(actual_days, 21) 
                df_master.loc[i - twap_days:i-1, 'MSTR_Hidden_Buy_BTC'] += (df_master.loc[i, 'MSTR_Bought'] / twap_days)
            last_buy_idx = i 
    df_master['MSTR_TWAP_USD_M'] = (df_master['MSTR_Hidden_Buy_BTC'] * df_master['BTC_Price']) / 1000000

    # 🌟 核心：计算全局当日总流入
    df_master['Global_Inflow'] = df_master['ETF_Net_Inflow'] + df_master['Stable_Change'] + df_master['MSTR_TWAP_USD_M']

    # 统一计算动力学指标 (Velocity斜率, Accel加速度)
    def add_momentum(df, col, prefix):
        df[f'{prefix}_Velocity'] = df[col].ewm(span=3, adjust=False).mean() - df[col].ewm(span=7, adjust=False).mean()
        df[f'{prefix}_Accel'] = df[f'{prefix}_Velocity'] - df[f'{prefix}_Velocity'].shift(1)

    add_momentum(df_master, 'Global_Inflow', 'Global')
    add_momentum(df_master, 'Stable_Change', 'Stable')
    add_momentum(df_master, 'USDT_Change', 'USDT')
    add_momentum(df_master, 'USDC_Change', 'USDC')
    add_momentum(df_master, 'MSTR_TWAP_USD_M', 'MSTR')

    return df_master

# ==========================================
# 📊 UI 渲染层
# ==========================================
st.title("🦅 流动性三层异构雷达系统")

with st.spinner('穿透抓取底层数据并演算全网资金动力学...'):
    df = load_and_process_data()

last_row = df.iloc[-1]
prev_row = df.iloc[-2]

# 画图辅助函数 (已优化纵坐标自适应缩放)
def plot_dual_charts(df_sub, date_col, total_col, flow_col, total_name, flow_name, color):
    fig = make_subplots(rows=1, cols=2, subplot_titles=(f"累计存量 ({total_name})", f"每日流量 ({flow_name})"))
    
    # 1. 累计存量图：去掉了 fill='tozeroy'，把线调粗一点
    fig.add_trace(go.Scatter(x=df_sub[date_col], y=df_sub[total_col], line=dict(color=color, width=3)), row=1, col=1)
    
    # 2. 每日流量图 (保持不变)
    colors = ['#2ca02c' if val > 0 else '#d62728' for val in df_sub[flow_col]]
    fig.add_trace(go.Bar(x=df_sub[date_col], y=df_sub[flow_col], marker_color=colors), row=1, col=2)
    
    # 3. 核心修复：强制左侧(累计图)的 Y 轴根据真实数据的极值自适应，不再强行从 0 开始
    fig.update_yaxes(autorange=True, fixedrange=False, row=1, col=1)
    
    fig.update_layout(height=350, showlegend=False, margin=dict(l=20, r=20, t=40, b=20))
    return fig

# ------------------------------------------
# 🥇 第一层：资金总览（全局宏观）
# ------------------------------------------
st.markdown("## 🥇 第一层：全网资金总览")
st.caption("把当日 ETF + USDC + USDT + 微策略(TWAP平摊) 的资金相加，计算出真实进入币圈的总美元火力。")

col_lvl1, col_lvl2 = st.columns([1, 3])
col_lvl1.metric("今日全网净流入总计", f"{last_row['Global_Inflow']:,.1f} M", f"{last_row['Global_Inflow'] - prev_row['Global_Inflow']:,.1f} M (较昨日)")

# 画全局图表
fig_global = make_subplots(specs=[[{"secondary_y": True}]])
colors_global = ['#2ca02c' if val > 0 else '#d62728' for val in df['Global_Inflow']]
fig_global.add_trace(go.Bar(x=df['Date'], y=df['Global_Inflow'], name="全网净流入 (M)", marker_color=colors_global), secondary_y=False)
fig_global.add_trace(go.Scatter(x=df['Date'], y=df['BTC_Price'], name="BTC 价格", line=dict(color='orange', width=2)), secondary_y=True)
fig_global.update_layout(title_text="全网真实净流入金额 VS BTC 价格", height=450, hovermode="x unified")
st.plotly_chart(fig_global, width='stretch')

st.markdown("---")

# ------------------------------------------
# 🥈 第二层：资金动力学（交易定性）
# ------------------------------------------
st.markdown("## 🥈 第二层：买卖盘定性与边际动能")
st.caption("基于 EMA(3)与EMA(7)一阶导数(斜率)和二阶导数(加速度)计算。不看绝对数值，只看资金的“边际倾向”。")

v = last_row['Global_Velocity']
a = last_row['Global_Accel']

if v > 0 and a > 0: signal, desc, alert = "🟢 买盘爆发 (加速流入)", "当前属于强势买方市场，且增量资金正在**加速进场**。", "success"
elif v > 0 and a < 0: signal, desc, alert = "🟡 买盘衰竭 (流入放缓)", "买盘仍在主导，但资金流入的**速度开始减缓**，警惕高位乏力。", "warning"
elif v < 0 and a < 0: signal, desc, alert = "🔴 卖盘爆发 (加速流出)", "当前属于弱势卖方市场，且资金正在**加速逃离**。", "error"
elif v < 0 and a > 0: signal, desc, alert = "🟠 卖盘衰竭 (流出减缓)", "市场仍在失血，但流出速度**已经放缓**，可能正在筑底。", "info"
else: signal, desc, alert = "⚪ 震荡寻底", "资金动能微弱，多空博弈激烈。", "info"

getattr(st, alert)(f"**核心判决：{signal}** —— {desc}")

c1, c2, c3 = st.columns(3)
c1.metric("短期斜率 (Velocity)", f"{v:,.1f} M/天", help="正数代表资金倾向流入，负数代表流出。")
c2.metric("变化率/加速度 (Acceleration)", f"{a:,.1f} M/天", f"{a:,.1f}", help="这一项极其关键！代表今天的斜率比昨天增加了还是减少了。")
c3.info(f"💡 若加速度为正 (+)，说明有新增买盘或抛压减轻；若为负 (-)，说明买盘减弱或卖盘增加。")

st.markdown("---")

# ------------------------------------------
# 🥉 第三层：数据模块拆解
# ------------------------------------------
st.markdown("## 🥉 第三层：底层数据模块拆解归因")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["ETF", "稳定币总计", "USDC", "USDT", "微策略"])

def render_module(tab, name, level, velocity, accel, df, date_col, total_col, flow_col, color):
    with tab:
        c1, c2, c3 = st.columns(3)
        c1.metric("当日当前值", f"{level:,.1f} M")
        c2.metric("趋势斜率", f"{velocity:,.1f}", f"{velocity:,.1f}")
        c3.metric("买卖加速度", f"{accel:,.1f}", f"{accel:,.1f}")
        st.plotly_chart(plot_dual_charts(df.tail(90), date_col, total_col, flow_col, f"{name}累计量", f"{name}每日变化", color), width='stretch')

# 1. ETF
render_module(tab1, "ETF", last_row['ETF_Net_Inflow'], last_row['ETF_Velocity'], last_row['ETF_Accel'], df, 'Date', 'ETF_Cumsum', 'ETF_Net_Inflow', '#1f77b4')

# 2. 稳定币总计
render_module(tab2, "总稳定币", last_row['Stable_Change'], last_row['Stable_Velocity'], last_row['Stable_Accel'], df, 'Date', 'Total_Stable_Mcap', 'Stable_Change', '#9467bd')

# 3. USDC
render_module(tab3, "USDC", last_row['USDC_Change'], last_row['USDC_Velocity'], last_row['USDC_Accel'], df, 'Date', 'USDC_Mcap', 'USDC_Change', '#2752B6')

# 4. USDT
render_module(tab4, "USDT", last_row['USDT_Change'], last_row['USDT_Velocity'], last_row['USDT_Accel'], df, 'Date', 'USDT_Mcap', 'USDT_Change', '#26A17B')

# 5. 微策略 (定制化展示)
with tab5:
    c1, c2, c3 = st.columns(3)
    c1.metric("今日推算隐形买盘 (TWAP)", f"{last_row['MSTR_TWAP_USD_M']:,.1f} M")
    c2.metric("建仓斜率 (Slope)", f"{last_row['MSTR_Velocity']:,.1f}")
    c3.metric("买盘加速度 (Accel)", f"{last_row['MSTR_Accel']:,.1f}")
    
    last_buy_row = df[df['MSTR_Bought'] > 0].iloc[-1] if not df[df['MSTR_Bought'] > 0].empty else None
    if last_buy_row is not None:
        days_ago = (pd.to_datetime(last_row['Date']) - pd.to_datetime(last_buy_row['Date'])).days
        st.info(f" **巨鲸动向：** 上次公开购买发生在 **{days_ago} 天前**，购买量为 **{last_buy_row['MSTR_Bought']:,.0f} 枚 BTC**。当前展示的每日流量为根据 21 天回溯期动态平摊的隐形建仓资金。")
        
    st.plotly_chart(plot_dual_charts(df.tail(90), 'Date', 'MSTR_Cumsum', 'MSTR_TWAP_USD_M', "微策略持币总量(枚)", "推算每日吸筹资金(M)", '#ff7f0e'), width='stretch')

st.markdown("<br><center><p style='color:gray;'>由您的专属 AI 量化架构师构建</p></center>", unsafe_allow_html=True)
