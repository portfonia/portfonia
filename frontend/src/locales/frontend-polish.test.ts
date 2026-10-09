import { expect, it } from "vitest";
import { catalogs } from "./index";

function valueAt(value: unknown, path: string): unknown {
  for (const key of path.split(".")) {
    value = Array.isArray(value) ? value[Number(key)] : (value as Record<string, unknown>)[key];
  }
  return value;
}
const expected = {
  "zh-Hans": {
    "menu.reports": "报告中心",
    "reports.title": "报告中心",
    "profile.reportScheduleOptions.everyOtherDay": "隔日",
    "reports.types.everyOtherDay": "隔日",
    "profile.subscriptionTitle.mwf": "隔日简报",
    "home.faq.items.3.a": "\u6bcf\u5468\u7b80\u62a5\u6bcf\u6708 0.99 credits \u8d77\uff0c\u9694\u65e5\u7b80\u62a5\uff08\u5468\u4e00\u3001\u5468\u4e09\u3001\u5468\u4e94\u53d1\u9001\uff09\u6bcf\u6708 1.99 credits\uff0c\u542b AI\u667a\u80fd\u4f53\u6570\u636e\u63a5\u53e3\u7684\u8fdb\u9636\u6bcf\u65e5\u7b80\u62a5\u6bcf\u6708 2.49 credits\uff0c\u6da6\u7389\u6bcf\u6708 9.99 credits\uff0c\u5728\u8fdb\u9636\u5957\u9910\u7684\u5168\u90e8\u529f\u80fd\u4e4b\u5916\u589e\u52a0\u6295\u8d44\u7ec4\u5408\u98ce\u9669\u5de5\u5177\u3002Credits \u4ee5 US$10 \u548c US$20 \u70b9\u6570\u5305\u51fa\u552e\u2014\u2014\u8be6\u89c1\u5b9a\u4ef7\u9875\u9762\u3002",
    "welcome.subscriptionPlans": "Portfonia \u5411\u8ba2\u9605\u7528\u6237\u53d1\u9001\u5b9a\u671f\u7b80\u62a5\u3002\u6709\u56db\u79cd\u5957\u9910\uff1a\u6bcf\u5468\u7b80\u62a5\uff08\u6bcf\u6708 0.99 credits\uff09\u3001\u9694\u65e5\u7b80\u62a5\uff08\u6bcf\u6708 1.99 credits\uff0c\u5468\u4e00\u3001\u5468\u4e09\u3001\u5468\u4e94\u53d1\u9001\uff09\u3001\u8fdb\u9636\u6bcf\u65e5\u7b80\u62a5\uff08\u6bcf\u6708 2.49 credits\uff0c\u5de5\u4f5c\u65e5\u53d1\u9001\uff0c\u542b AI\u667a\u80fd\u4f53\u6570\u636e\u63a5\u53e3\uff09\u548c\u6da6\u7389\uff08\u6bcf\u6708 9.99 credits\uff0c\u6295\u8d44\u7ec4\u5408\u98ce\u9669\u5de5\u5177\u3001\u8fdb\u9636\u5168\u90e8\u529f\u80fd\u548c\u81ea\u9009\u7b80\u62a5\u9891\u7387\uff09\u3002\u6da6\u7389\u53ef\u5728\u6da6\u7389\u5e73\u53f0\u8ba2\u9605\u3002",
    "legal.pricing.sections.0.body.1": "隔日简报——每月 1.99 credits。每周三封个性化简报邮件（周一、周三、周五）。",
    "home.how.cards.2.body": "按每周、隔日或每日推送，紧扣你的真实持仓——包含价格异动、技术面位置，以及宏观与财报的前瞻日历。",
    "legal.pricing.sections.0.body.4": "\u6bcf\u5468\u4e0e\u9694\u65e5\u4e24\u6863\u7684\u533a\u522b\u4ec5\u5728\u4e8e\u53d1\u9001\u9891\u7387\u3002\u6bcf\u65e5\u4e3a\u8fdb\u9636\u5957\u9910\u3002\u6bcf\u65e5\u4e0e\u6da6\u7389\u5747\u5141\u8bb8\u60a8\u7684 AI\u667a\u80fd\u4f53\u8bfb\u53d6\u9010\u65e5\u6301\u4ed3\u5feb\u7167\u548c\u4e0e\u6301\u4ed3\u76f8\u5173\u7684\u60c5\u62a5\u3002\u6240\u6709\u5957\u9910\u90fd\u53ef\u4ee5\u8ba9 AI\u667a\u80fd\u4f53\u8bfb\u53d6\u5386\u53f2\u62a5\u544a\u3002"
  },
  "zh-Hant": {
    "menu.reports": "報告中心",
    "reports.title": "報告中心",
    "profile.reportScheduleOptions.everyOtherDay": "隔日",
    "reports.types.everyOtherDay": "隔日",
    "profile.subscriptionTitle.mwf": "隔日簡報",
    "home.faq.items.3.a": "\u6bcf\u9031\u7c21\u5831\u6bcf\u6708 0.99 credits \u8d77\uff0c\u9694\u65e5\u7c21\u5831\uff08\u9031\u4e00\u3001\u9031\u4e09\u3001\u9031\u4e94\u5bc4\u9001\uff09\u6bcf\u6708 1.99 credits\uff0c\u542b AI \u667a\u80fd\u9ad4\u8cc7\u6599\u4ecb\u9762\u7684\u9032\u968e\u6bcf\u65e5\u7c21\u5831\u6bcf\u6708 2.49 credits\uff0c\u6f64\u7389\u6bcf\u6708 9.99 credits\uff0c\u5305\u542b\u9032\u968e\u65b9\u6848\u6240\u6709\u529f\u80fd\u4e26\u589e\u6dfb\u6295\u8cc7\u7d44\u5408\u98a8\u96aa\u5de5\u5177\u3002Credits \u4ee5 US$10 \u8207 US$20 \u9ede\u6578\u5305\u51fa\u552e\u2014\u2014\u8a73\u898b\u5b9a\u50f9\u9801\u9762\u3002",
    "welcome.subscriptionPlans": "Portfonia \u5411\u8a02\u95b1\u4f7f\u7528\u8005\u5bc4\u9001\u5b9a\u671f\u7c21\u5831\u3002\u6709\u56db\u7a2e\u65b9\u6848\uff1a\u6bcf\u9031\u7c21\u5831\uff08\u6bcf\u6708 0.99 credits\uff09\u3001\u9694\u65e5\u7c21\u5831\uff08\u6bcf\u6708 1.99 credits\uff0c\u9031\u4e00\u3001\u9031\u4e09\u3001\u9031\u4e94\u5bc4\u9001\uff09\u3001\u9032\u968e\u6bcf\u65e5\u7c21\u5831\uff08\u6bcf\u6708 2.49 credits\uff0c\u5de5\u4f5c\u65e5\u5bc4\u9001\uff0c\u542b AI \u667a\u80fd\u9ad4\u8cc7\u6599\u4ecb\u9762\uff09\u548c\u6f64\u7389\uff08\u6bcf\u6708 9.99 credits\uff0c\u6295\u8cc7\u7d44\u5408\u98a8\u96aa\u5de5\u5177\u3001\u9032\u968e\u6240\u6709\u529f\u80fd\u8207\u81ea\u9078\u7c21\u5831\u6392\u7a0b\uff09\u3002\u8acb\u81f3\u6f64\u7389\u5e73\u53f0\u8a02\u95b1\u6f64\u7389\u3002",
    "legal.pricing.sections.0.body.1": "隔日簡報——每月 1.99 credits。每週三封個人化簡報郵件（週一、週三、週五）。",
    "home.how.cards.2.body": "按每週、隔日或每日推送，緊扣你的實際持倉——包含價格異動、技術面位置，以及總經與財報的前瞻日曆。",
    "legal.pricing.sections.0.body.4": "\u6bcf\u9031\u8207\u9694\u65e5\u65b9\u6848\u50c5\u6709\u5bc4\u9001\u983b\u7387\u7684\u5dee\u7570\u3002\u6bcf\u65e5\u662f\u9032\u968e\u65b9\u6848\u3002\u6bcf\u65e5\u8207\u6f64\u7389\u90fd\u53ef\u8b93\u60a8\u7684 AI \u667a\u80fd\u9ad4\u8b80\u53d6\u9010\u65e5\u6301\u5009\u5feb\u7167\u53ca\u6301\u5009\u76f8\u95dc\u60c5\u5831\u3002\u6bcf\u7a2e\u65b9\u6848\u7686\u53ef\u8b93 AI \u667a\u80fd\u9ad4\u8b80\u53d6\u6b77\u53f2\u5831\u544a\u3002"
  }
};
const english = {
  "menu.reports": "Reports",
  "reports.title": "Reports",
  "profile.reportScheduleOptions.everyOtherDay": "Mon/Wed/Fri",
  "reports.types.everyOtherDay": "Mon/Wed/Fri",
  "profile.subscriptionTitle.mwf": "Mon/Wed/Fri briefing",
  "home.faq.items.3.a": "Plans start at 0.99 credits per month for a weekly briefing, 1.99 for Monday / Wednesday / Friday briefings, and 2.49 for the Advanced Daily plan with AI Agent data access, and 9.99 for Jade, which adds portfolio risk tools to everything in the Advanced plan. Credits are sold in US$10 and US$20 packs \u2014 see Pricing.",
  "welcome.subscriptionPlans": "Portfonia sends scheduled briefings to subscribers. There are four plans: Weekly (0.99 credits per month), Mon/Wed/Fri (1.99 credits per month), the Advanced Daily plan (2.49 credits per month, weekdays, with AI Agent data access), and Jade (9.99 credits per month, portfolio risk tools, everything in Advanced, and a briefing schedule you choose). Subscribe to Jade on the Jade page.",
  "legal.pricing.sections.0.body.1": "Mon / Wed / Fri briefing \u2014 1.99 credits per month. Three personalized briefing emails per week (Monday, Wednesday, Friday).",
  "home.how.cards.2.body": "Mon / Wed / Fri, tied to your actual holdings \u2014 price anomalies, technical position, and a forward calendar of macro and earnings events.",
  "legal.pricing.sections.0.body.4": "Weekly and Mon / Wed / Fri differ only in how often briefings are sent. Daily is the Advanced plan. Daily and Jade both let your AI agent read your daily holding snapshots and holding-related intelligence. Report history is available to AI agents on every plan."
};
it.each(["zh-Hans", "zh-Hant"] as const)("polish_660_acceptance_4 exact Chinese labels in %s", locale => {
  for (const [path, value] of Object.entries(expected[locale])) {
    expect(valueAt(catalogs[locale], path), path).toBe(value);
    expect(valueAt(catalogs[locale], path), path).not.toMatch(/一、三、五|一 \/ 三 \/ 五/);
  }
});
it("polish_660_acceptance_4 preserves English copy", () => {
  for (const [path, value] of Object.entries(english)) expect(valueAt(catalogs.en, path), path).toBe(value);
});
