export type Locale = "en" | "zh";

export const locales: { value: Locale; label: string }[] = [
  { value: "en", label: "English" },
  { value: "zh", label: "简体中文" },
];

interface HomeMessages {
  nav: {
    boundary: string;
    how: string;
    preview: string;
    faq: string;
    cta: string;
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
    distributionLabel: string;
    distribution: { label: string; pct: number }[];
    highlights: {
      text: string;
      tier: "established" | "probable" | "speculative";
    }[];
    calendarChip: string;
  };
  boundary: { heading: string; body: string; items: string[] };
  faq: { heading: string; tag: string; items: { q: string; a: string }[] };
  status: string;
  footer: { stack1: string; stack2: string };
}

export const homeMessages: Record<Locale, HomeMessages> = {
  en: {
    nav: {
      boundary: "What we don't do",
      how: "How it works",
      preview: "Sample briefing",
      faq: "FAQ",
      cta: "Get started",
      language: "Language",
      login: "Log in",
      logout: "Log out",
    },
    hero: {
      eyebrow: "Ring 0 · single-user prototype",
      titleLine1: "What's worth noticing",
      titleAccent: "in your holdings.",
      sub: "Portfonia watches the market and maps it back to your actual positions — US equities, HK equities, A-shares, funds, cash, and FX. It tells you what changed and why it matters. It never tells you what to do.",
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
      tag: "Illustrative example",
      badge: "Sample data — not a real portfolio",
      distributionLabel: "Asset-class distribution",
      distribution: [
        { label: "US Equity", pct: 42 },
        { label: "HK Equity", pct: 18 },
        { label: "A-Share", pct: 15 },
        { label: "Cash & FX", pct: 13 },
        { label: "Gold", pct: 7 },
        { label: "Bond Fund", pct: 5 },
      ],
      highlights: [
        {
          text: "NVDA — single-session move of -6.2%, tied to a post-earnings guidance revision.",
          tier: "established",
        },
        {
          text: "USD/CNY drifting toward 7.30 as rate-differential commentary firms up.",
          tier: "probable",
        },
      ],
      calendarChip: "Forward calendar flags events like FOMC decisions ahead of time",
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
          a: "No. Portfonia is an intelligence service — it tells you what happened and why it might matter, never what to do. See “Deliberately out of scope” above.",
        },
        {
          q: "Is my holdings data safe?",
          a: "Not yet encrypted at rest — that's planned before any public rollout. Today: LLM calls run with training-data collection denied by default, and each call only sees the data a given report actually needs — never the whole portfolio wholesale.",
        },
        {
          q: "Which markets and holdings does it support?",
          a: "US equities, HK equities, A-shares, Chinese public funds, cash, and FX today. More coverage is planned.",
        },
        {
          q: "What does it cost?",
          a: "Portfonia is currently a single-user prototype (Ring 0) validating whether this is useful at all — pricing hasn't been decided yet.",
        },
      ],
    },
    status:
      "Ring 0 — single-user local prototype, validating whether AI-mapped market context creates real cognitive lift before any public rollout. AI-generated content, for information only — not investment advice.",
    footer: {
      stack1: "Next.js · FastAPI · Celery · PostgreSQL",
      stack2: "Pluggable LLM providers via OpenRouter",
    },
  },
  zh: {
    nav: {
      boundary: "我们不做什么",
      how: "工作原理",
      preview: "示例简报",
      faq: "常见问题",
      cta: "开始使用",
      language: "语言",
      login: "登录",
      logout: "退出登录",
    },
    hero: {
      eyebrow: "Ring 0 · 单用户原型",
      titleLine1: "什么值得关注",
      titleAccent: "就藏在你的持仓里。",
      sub: "Portfonia 持续关注市场动态，并把它们对应到你的真实持仓——美股、港股、A股、公募基金、现金与外汇。我们会告诉你发生了什么变化、为什么重要，但从不告诉你该怎么做。",
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
      tag: "示例说明",
      badge: "示例数据——非真实持仓",
      distributionLabel: "资产类别分布",
      distribution: [
        { label: "美股", pct: 42 },
        { label: "港股", pct: 18 },
        { label: "A股", pct: 15 },
        { label: "现金与外汇", pct: 13 },
        { label: "黄金", pct: 7 },
        { label: "债券基金", pct: 5 },
      ],
      highlights: [
        {
          text: "英伟达（NVDA）单日 -6.2%，与财报后指引下修相关。",
          tier: "established",
        },
        {
          text: "美元兑人民币汇率向 7.30 靠拢，市场消化利差相关表态。",
          tier: "probable",
        },
      ],
      calendarChip: "前瞻日历会提前标注 FOMC 议息决议这类宏观事件",
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
          a: "不是。Portfonia 是情报服务——只告诉你发生了什么、为什么可能重要，从不告诉你该怎么做。见上方“明确不做的事”。",
        },
        {
          q: "我的持仓数据安全吗？",
          a: "目前尚未静态加密存储——这是公开上线前会补上的一项。现状：LLM 调用默认禁止被用作训练数据，且每次只发送当次报告实际需要的范围——不会把完整持仓整体发给第三方。",
        },
        {
          q: "支持哪些市场和持仓类型？",
          a: "目前支持美股、港股、A股、中国公募基金、现金与外汇，后续会扩展覆盖范围。",
        },
        {
          q: "收费吗？",
          a: "Portfonia 目前是单用户原型（Ring 0），还在验证这件事本身是否有价值——定价尚未确定。",
        },
      ],
    },
    status:
      "Ring 0——单用户本地原型，目标是验证一个假设：AI 把市场信息对应到个人持仓上，能否带来经纪商 App、财经媒体或普通订阅通讯给不到的认知增量。AI 生成内容仅供参考，不构成投资建议。",
    footer: {
      stack1: "Next.js · FastAPI · Celery · PostgreSQL",
      stack2: "通过 OpenRouter 接入可插拔的 LLM 提供方",
    },
  },
};
