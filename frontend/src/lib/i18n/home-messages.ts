export type Locale = "en" | "zh";

export const locales: { value: Locale; label: string }[] = [
  { value: "en", label: "English" },
  { value: "zh", label: "简体中文" },
];

interface HomeMessages {
  nav: { boundary: string; how: string; cta: string; language: string };
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
  boundary: { heading: string; body: string; items: string[] };
  status: string;
  footer: { stack1: string; stack2: string };
}

export const homeMessages: Record<Locale, HomeMessages> = {
  en: {
    nav: {
      boundary: "What we don't do",
      how: "How it works",
      cta: "Get started",
      language: "Language",
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
      cta: "开始使用",
      language: "语言",
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
    status:
      "Ring 0——单用户本地原型，目标是验证一个假设：AI 把市场信息对应到个人持仓上，能否带来经纪商 App、财经媒体或普通订阅通讯给不到的认知增量。AI 生成内容仅供参考，不构成投资建议。",
    footer: {
      stack1: "Next.js · FastAPI · Celery · PostgreSQL",
      stack2: "通过 OpenRouter 接入可插拔的 LLM 提供方",
    },
  },
};
