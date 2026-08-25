export type Locale = "en" | "zh";

export const locales: { value: Locale; label: string }[] = [
  { value: "en", label: "English" },
  { value: "zh", label: "简体中文" },
];

interface HomeMessages {
  nav: {
    menu: string;
    language: string;
    login: string;
    logout: string;
  };
  hero: {
    eyebrow: string;
    titleLine1: string;
    titleAccent: string;
    sub: string;
    ctaPrimary: string;
    ctaSecondary: string;
  };
  how: {
    heading: string;
    tag: string;
    cards: { title: string; body: string; tags: string[] }[];
    confidenceLead: string;
    confidenceBody: string;
    tiers: { established: string; probable: string; speculative: string };
  };
  preview: {
    heading: string;
    tag: string;
    badge: string;
    // Closing note under the sample (owner ask, issue #207 follow-up)
    footnote: string;
    snapshotTitle: string;
    totalLine: string;
    holdingsColumns: string[];
    holdingsRows: string[][];
    distributionLines: string[];
    macroTitle: string;
    macroBody: string;
    calendarTitle: string;
    calendarNote: string;
    calendarColumns: string[];
    calendarRows: string[][];
    analysisTitle: string;
    analysisHeading: string;
    analysisBody: string;
    radarTitle: string;
    concentrationBody: string;
    anomalyColumns: string[];
    anomalyRows: string[][];
    anomalyBullets: string[];
    technicalColumns: string[];
    technicalRows: string[][];
    technicalNote: string;
  };
  boundary: { heading: string; body: string; items: string[] };
  faq: { heading: string; tag: string; items: { q: string; a: string }[] };
  status: string;
  footer: { stack1: string; stack2: string };
}

export const homeMessages: Record<Locale, HomeMessages> = {
  en: {
    nav: {
      menu: "Get Started",
      language: "Language",
      login: "Log in",
      logout: "Log out",
    },
    hero: {
      eyebrow: "MVP · Multi-user closed beta",
      titleLine1: "What's worth noticing",
      titleAccent: "in your holdings.",
      sub: "Portfonia watches the market and maps it back to your actual positions — US equities, HK equities, A-shares, funds, cash, and FX. It tells you what changed and why it matters, helping you make timely, well-informed decisions.",
      ctaPrimary: "Get started",
      ctaSecondary: "See how it works",
    },
    how: {
      heading: "How it works",
      tag: "Three moving parts",
      cards: [
        {
          title: "Holdings ingestion",
          body: "Upload a CSV or spreadsheet describing your positions. An LLM normalizes it into structured records — no manual re-entry.",
          tags: ["CSV", "Markdown", "Excel"],
        },
        {
          title: "Market & macro tracking",
          body: "Daily price, FX, and curated macro-news scanning across reputable sources — English-language primary, Chinese-language where the instrument calls for it.",
          tags: ["Prices", "FX", "Macro news"],
        },
        {
          title: "Personalized briefings",
          body: "Mon / Wed / Fri, tied to your actual holdings — price anomalies, technical position, and a forward calendar of macro and earnings events.",
          tags: ["Email delivery"],
        },
      ],
      confidenceLead: "Every causal claim ships with a confidence label",
      confidenceBody:
        "Calibrated uncertainty, made legible instead of hidden.",
      tiers: { established: "Established", probable: "Probable", speculative: "Speculative" },
    },
    preview: {
      heading: "What a briefing actually looks like",
      tag: "Real report, anonymized",
      badge: "Anonymized from a real report — sample holdings & figures",
      footnote:
        "Actual content varies depending on your subscription tier.",
      snapshotTitle: "Portfolio snapshot",
      totalLine: "Total value: $1,284,600 (FX date: 2026-08-21)",
      // [name (ticker), currency, value, weight, custodian, asset class]
      holdingsColumns: ["Holding", "Currency", "Value", "Weight", "Custodian", "Asset class"],
      holdingsRows: [
        ["TSMC (TSM)", "USD", "268,400", "20.9%", "Custodian A", "Stocks"],
        ["Microsoft (MSFT)", "USD", "102,900", "8.0%", "Custodian A", "Stocks"],
        ["Alphabet (GOOGL)", "USD", "88,150", "6.9%", "Custodian A", "Stocks"],
        ["**Custodian A subtotal**", "USD", "**459,450**", "**35.7%**", "", ""],
        ["QQQ (QQQ)", "USD", "205,300", "16.0%", "Custodian B", "US tech ETF"],
        ["VOO (VOO)", "USD", "168,700", "13.1%", "Custodian B", "US broad ETF"],
        ["BOXX (BOXX)", "USD", "61,800", "4.8%", "Custodian B", "Bond fund"],
        ["Cash", "USD", "2,400", "0.2%", "Custodian B", "Cash equivalent"],
        ["**Custodian B subtotal**", "USD", "**438,200**", "**34.1%**", "", ""],
        ["Tencent (0700.HK)", "HKD", "118,300", "7.3%", "Custodian C", "Stocks"],
        ["BYD (1211.HK)", "HKD", "64,200", "5.0%", "Custodian C", "Stocks"],
        ["**Custodian C subtotal**", "USD", "**23,350**", "**12.3%**", "", ""],
        ["Gold ETF (518880.SS)", "CNH", "9,100", "0.6%", "Custodian D", "Precious metals"],
        ["CSI 300 ETF (510300.SS)", "CNH", "6,400", "0.4%", "Custodian D", "China equities"],
        ["**Custodian D subtotal**", "USD", "**1,900**", "**1.0%**", "", ""],
      ],
      distributionLines: [
        "**By market:** US 69.8%, HK 12.3%, A-shares 1.0%, Cash & FX 16.9%",
        "**By currency:** USD 69.8%, HKD 12.3%, CNH 1.0%",
        "**By asset class:** Stocks 48.1%, US broad ETF 13.1%, US tech ETF 16.0%, Bond funds 4.8%, Precious metals 0.6%, Cash equivalents 17.4%",
      ],
      macroTitle: "Macro signals",
      macroBody:
        "Markets repriced the policy path after the latest FOMC minutes showed a split committee. The transmission into this portfolio runs through the equity-duration channel: long-duration growth names (TSMC, Microsoft, Alphabet) and the broad ETFs are mechanically sensitive to shifts in rate expectations embedded in the yield curve. No price anomaly was triggered in this window to confirm a repricing in any specific holding, so this direction stands as structural exposure to monitor rather than an observed event. [Probable]",
      calendarTitle: "Forward calendar",
      calendarNote:
        "Scheduled US events over the coming days and the holdings they bear on. Calendar facts only — not forecasts.",
      calendarColumns: ["Date", "Event", "Affected holdings", "What to watch"],
      calendarRows: [
        ["08-27", "Jobless claims", "—", "Labor market momentum; rate-path implications"],
        ["08-29", "Core PCE price index", "TSMC, MSFT, GOOGL +2", "Inflation vs consensus; rate-path implications"],
        ["09-02", "ISM Manufacturing PMI", "—", "Factory-sector momentum"],
      ],
      analysisTitle: "Holding analysis",
      analysisHeading: "TSMC — 20.9% of the portfolio, the single largest position",
      analysisBody:
        "TSMC stands out this period not for a company-specific event but for its disproportionate weight and its exposure to several macro themes at once. The monetary-policy theme reaches it through the equity-duration channel: as a pure-play foundry, its forward multiples already embed years of capex-driven growth from advanced nodes, so its valuation is mechanically sensitive to rate expectations. The AI-infrastructure demand narrative underpinning the position is unchanged; what is new is a less predictable policy path adding an intermittent source of valuation volatility. The causal link between holding TSMC and the monetary-policy signal runs through that well-documented duration mechanism [Probable] — though no anomaly was observed in this window to confirm the market actually traded on it.",
      radarTitle: "Risk radar",
      concentrationBody:
        "The top three holdings together account for 45.8% of portfolio value — below the 50% watch threshold. The largest single asset-class bucket (stocks) sits at 48.1%, also below threshold. Single-position concentration is dominated by TSMC at 20.9%; the two broad ETF positions diversify within US equities while raising correlation to US monetary-policy developments.",
      anomalyColumns: [
        "Holding",
        "Net move",
        "Worst day (date)",
        "Prev close",
        "Open (gap %)",
        "Intraday range",
        "Close",
        "Trigger",
      ],
      anomalyRows: [
        ["NVDA (NVDA)", "+8.4%", "+5.2% (08-25)", "181.90", "183.40 (+0.8%)", "182.1–191.6", "190.95", "single_day"],
        ["ORCL (ORCL)", "+6.1%", "+4.7% (08-24)", "144.30", "143.9 (-0.3%)", "142.5–151.0", "150.85", "single_day"],
      ],
      anomalyBullets: [
        "NVDA — +8.4% over the window; the +5.2% single-day surge came with no immediately identifiable company-specific catalyst in this period's research; attribution remains open [Speculative].",
        "ORCL — +6.1% over the window; coincided with an active AI-sector news cycle, but no Oracle-specific driver was confirmed in research [Probable].",
      ],
      technicalColumns: ["Holding", "vs 50-day avg", "vs 200-day avg", "52-wk range position", "20-day vol (ann.)"],
      technicalRows: [
        ["TSMC (TSM)", "-2.5%", "+16.0%", "75%", "+45.3%"],
        ["Microsoft (MSFT)", "+20.9%", "+12.9%", "73%", "+61.1%"],
        ["Alphabet (GOOGL)", "+1.4%", "+10.7%", "81%", "+50.9%"],
        ["QQQ (QQQ)", "+0.5%", "+11.2%", "86%", "+26.5%"],
        ["VOO (VOO)", "+3.3%", "+10.2%", "99%", "+14.4%"],
      ],
      technicalNote:
        "> Range position: 0% = at the 52-week low, 100% = at the high. Describes where price sits — not a signal.",
    },
    boundary: {
      heading: "Deliberately out of scope",
      body: "Portfonia is an intelligence service, not an advisory one. The boundary is enforced at the template and prompt layer — not left to the model's judgment.",
      items: [
        "No buy / sell / hold / reduce / increase / target-price language. Ever.",
        "No trade execution, no broker integrations beyond ingest-only.",
        "No tax or capital-gains computation, no P&L from trade history.",
        "No options, futures, or derivatives.",
        "No threshold price alerts — every broker app already does that.",
        "No social or sharing features — holdings are sensitive data.",
      ],
    },
    faq: {
      heading: "Frequently asked",
      tag: "Before you ask",
      items: [
        {
          q: "Is this investment advice?",
          a: "No. Portfonia is an intelligence service — it tells you what happened and why it might matter, helping you make timely, well-informed decisions. See “Deliberately out of scope” above.",
        },
        {
          q: "Is my holdings data safe?",
          a: "Yes — your holdings are strongly encrypted at rest (field-level encryption in our database). On top of that, AI calls run with training-data collection denied by default, and each call only sees the data a given report actually needs — never the whole portfolio wholesale.",
        },
        {
          q: "Which markets and holdings does it support?",
          a: "US equities, HK equities, A-shares, Chinese public funds, cash, and FX today. More coverage is planned.",
        },
        {
          q: "What does it cost?",
          a: "Portfonia is in a multi-user closed beta right now — content and features may differ by subscription tier when plans are announced.",
        },
      ],
    },
    status:
      "MVP — multi-user closed beta. Portfonia maps market context back to your real holdings so you can make timely, well-informed decisions. AI-generated content, for information only — not investment advice.",
    footer: {
      stack1: "Next.js · FastAPI · Celery · PostgreSQL",
      stack2: "Pluggable LLM providers via OpenRouter",
    },
  },
  zh: {
    nav: {
      menu: "开始使用",
      language: "语言",
      login: "登录",
      logout: "退出登录",
    },
    hero: {
      eyebrow: "MVP · 多用户封闭测试",
      titleLine1: "什么值得关注",
      titleAccent: "就藏在你的持仓里。",
      sub: "Portfonia 持续关注市场动态，并把它们对应到你的真实持仓——美股、港股、A股、公募基金、现金与外汇。我们告诉你发生了什么变化、为什么重要，帮助你作出及时明智的决策。",
      ctaPrimary: "开始使用",
      ctaSecondary: "了解工作原理",
    },
    how: {
      heading: "工作原理",
      tag: "三个环节",
      cards: [
        {
          title: "持仓录入",
          body: "上传 CSV 或表格描述你的持仓，AI 会自动整理成结构化数据——不用手动逐条录入。",
          tags: ["CSV", "Markdown", "Excel"],
        },
        {
          title: "市场与宏观追踪",
          body: "每日追踪价格、汇率，并在权威信源中扫描相关宏观新闻——以英文信源为主，涉及区域性标的时补充中文信源。",
          tags: ["价格", "汇率", "宏观新闻"],
        },
        {
          title: "个性化简报",
          body: "每周一、三、五推送，紧扣你的真实持仓——包含价格异动、技术面位置，以及宏观与财报的前瞻日历。",
          tags: ["邮件推送"],
        },
      ],
      confidenceLead: "每一条因果归因都标有置信度标签",
      confidenceBody: "把校准过的不确定性讲清楚，而不是藏起来。",
      // Canonical zh-Hans renderings from backend/config/i18n_glossary.yml's
      // report_glossary ([Established]/[Probable]/[Speculative]), brackets
      // dropped to match this page's bracket-free English labels.
      tiers: { established: "确定", probable: "较可能", speculative: "推测" },
    },
    preview: {
      heading: "简报实际长什么样",
      tag: "真实报告·已脱敏",
      badge: "取自真实报告并脱敏——标的与数字为示例",
      footnote: "具体内容依订阅版本不同而有所差别。",
      snapshotTitle: "投资组合快照",
      totalLine: "总价值：1,284,600 美元（外汇日期：2026-08-21）",
      holdingsColumns: ["持仓", "货币", "价值", "持仓占比", "托管机构", "资产类别"],
      holdingsRows: [
        ["台积电 (TSM)", "美元", "268,400", "20.9%", "机构 A", "股票"],
        ["微软 (MSFT)", "美元", "102,900", "8.0%", "机构 A", "股票"],
        ["谷歌 (GOOGL)", "美元", "88,150", "6.9%", "机构 A", "股票"],
        ["**机构 A 小计**", "美元", "**459,450**", "**35.7%**", "", ""],
        ["纳指100ETF (QQQ)", "美元", "205,300", "16.0%", "机构 B", "美国科技股ETF"],
        ["标普500ETF (VOO)", "美元", "168,700", "13.1%", "机构 B", "美国宽基ETF"],
        ["BOXX (BOXX)", "美元", "61,800", "4.8%", "机构 B", "债券基金"],
        ["现金", "美元", "2,400", "0.2%", "机构 B", "现金等价物"],
        ["**机构 B 小计**", "美元", "**438,200**", "**34.1%**", "", ""],
        ["腾讯控股 (0700.HK)", "港元", "118,300", "7.3%", "机构 C", "股票"],
        ["比亚迪股份 (1211.HK)", "港元", "64,200", "5.0%", "机构 C", "股票"],
        ["**机构 C 小计**", "美元", "**23,350**", "**12.3%**", "", ""],
        ["黄金ETF (518880.SS)", "人民币", "9,100", "0.6%", "机构 D", "贵金属"],
        ["沪深300ETF (510300.SS)", "人民币", "6,400", "0.4%", "机构 D", "中国股票"],
        ["**机构 D 小计**", "美元", "**1,900**", "**1.0%**", "", ""],
      ],
      distributionLines: [
        "**按市场：** 美国 69.8%，香港 12.3%，A股 1.0%，现金与外汇 16.9%",
        "**按货币：** 美元 69.8%，港元 12.3%，人民币 1.0%",
        "**按资产类别：** 股票 48.1%，美国宽基ETF 13.1%，美国科技股ETF 16.0%，债券基金 4.8%，贵金属 0.6%，现金等价物 17.4%",
      ],
      macroTitle: "宏观信号",
      macroBody:
        "最新 FOMC 会议纪要显示委员会内部分歧，市场随之重新定价政策路径。向本投资组合的传导经由权益久期渠道运行：长久期成长股（台积电、微软、谷歌）与宽基 ETF 的估值对收益率曲线中隐含利率预期的变化具有机械敏感性。本报告期内无价格异常触发以确认任何具体持仓发生了重新定价，因此该方向作为结构性敞口进行监测，而非已观察事件。[较可能]",
      calendarTitle: "未来日历",
      calendarNote: "未来数日美国预定事件及受影响的持仓。以下为日历事实，非预测。",
      calendarColumns: ["日期", "事件", "受影响持仓", "关注要点"],
      calendarRows: [
        ["08-27", "初请失业金人数", "—", "劳动力市场动能；利率路径影响"],
        ["08-29", "核心 PCE 物价指数", "台积电、微软、谷歌 等", "通胀数据与市场共识对比；利率路径影响"],
        ["09-02", "ISM 制造业 PMI", "—", "制造业动能"],
      ],
      analysisTitle: "持仓分析",
      analysisHeading: "台积电 — 占投资组合 20.9%，第一大单一持仓",
      analysisBody:
        "台积电在本报告期内并非因公司特定事件而凸显，而是因其不成比例的投资组合权重及其同时暴露于多重宏观主题。货币政策主题通过权益久期渠道影响它：作为纯晶圆代工厂，其远期估值倍数已包含先进制程多年资本支出驱动增长的预期，因此对利率预期变化具有机械敏感性。支撑该持仓的 AI 基础设施需求叙事并未改变；新出现的是一条更难预测的政策路径，为间歇性的估值波动引入了新来源。持有台积电与货币政策信号之间的因果联系经由上述已被充分验证的久期机制 [较可能]——尽管本窗口期内未观察到异常以确认市场确实就此交易。",
      radarTitle: "风险雷达",
      concentrationBody:
        "前三大持仓合计占投资组合价值的 45.8%——低于 50% 的警戒阈值。最大单一资产类别（股票）占比 48.1%，同样低于警戒线。个股集中度由台积电主导（20.9%）；两只宽基 ETF 在美股内部提供了分散化，同时放大了对美国货币政策走向的相关性风险。",
      anomalyColumns: [
        "持仓",
        "净占比",
        "最差单日（日期）",
        "前收盘",
        "开盘（缺口%）",
        "日内区间",
        "收盘",
        "触发条件",
      ],
      anomalyRows: [
        ["英伟达 (NVDA)", "+8.4%", "+5.2% (08-25)", "181.90", "183.40 (+0.8%)", "182.1–191.6", "190.95", "single_day"],
        ["甲骨文 (ORCL)", "+6.1%", "+4.7% (08-24)", "144.30", "143.9 (-0.3%)", "142.5–151.0", "150.85", "single_day"],
      ],
      anomalyBullets: [
        "英伟达 — 窗口期回报率 +8.4%；+5.2% 的单日飙升在本期研究中未发现可立即识别的公司特定催化剂；归因尚不明确 [推测]。",
        "甲骨文 — 窗口期回报率 +6.1%；恰逢活跃的 AI 板块新闻周期，但研究中未确认有甲骨文特定驱动因素 [较可能]。",
      ],
      technicalColumns: ["持仓", "相对50日均线", "相对200日均线", "52周区间位置", "20日波动率（年化）"],
      technicalRows: [
        ["台积电 (TSM)", "-2.5%", "+16.0%", "75%", "+45.3%"],
        ["微软 (MSFT)", "+20.9%", "+12.9%", "73%", "+61.1%"],
        ["谷歌 (GOOGL)", "+1.4%", "+10.7%", "81%", "+50.9%"],
        ["纳指100ETF (QQQ)", "+0.5%", "+11.2%", "86%", "+26.5%"],
        ["标普500ETF (VOO)", "+3.3%", "+10.2%", "99%", "+14.4%"],
      ],
      technicalNote: "> 区间位置：0% = 处于52周低点，100% = 处于52周高点。数据描述价格所处位置，并非信号。",
    },
    boundary: {
      heading: "明确不做的事",
      body: "Portfonia 是情报服务，不是投顾服务。这条边界在模板和 prompt 层强制执行——不依赖模型自己判断。",
      items: [
        "不出现买入 / 卖出 / 持有 / 减仓 / 加仓 / 目标价这类措辞。绝不会有。",
        "不做交易执行，除录入外不对接券商。",
        "不做税务或资本利得计算，不基于交易记录算盈亏。",
        "不涉及期权、期货或衍生品。",
        "不做阈值价格提醒（如“跌了 5%”）——每个券商 App 都已经在做这件事。",
        "早期阶段不做社交或分享功能——持仓是敏感数据。",
      ],
    },
    faq: {
      heading: "常见问题",
      tag: "先说清楚",
      items: [
        {
          q: "这是投资建议吗？",
          a: "不是。Portfonia 是情报服务——告诉你发生了什么、为什么可能重要，帮助你作出及时明智的决策。见上方“明确不做的事”。",
        },
        {
          q: "我的持仓数据安全吗？",
          a: "安全——你的持仓数据已静态强加密存储（数据库字段级加密）。此外，AI 调用默认禁止数据被用作训练，且每次只发送当次报告实际需要的范围——不会把完整持仓整体发给第三方。",
        },
        {
          q: "支持哪些市场和持仓类型？",
          a: "目前支持美股、港股、A股、中国公募基金、现金与外汇，后续会扩展覆盖范围。",
        },
        {
          q: "收费吗？",
          a: "Portfonia 目前处于多用户封闭测试阶段——正式方案公布时，内容与功能可能因订阅版本不同而有所差别。",
        },
      ],
    },
    status:
      "MVP——多用户封闭测试。Portfonia 把市场信息对应到你的真实持仓，帮助你作出及时明智的决策。AI 生成内容仅供参考，不构成投资建议。",
    footer: {
      stack1: "Next.js · FastAPI · Celery · PostgreSQL",
      stack2: "通过 OpenRouter 接入可插拔的 LLM 提供方",
    },
  },
};
