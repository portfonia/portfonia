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
    "home.faq.items.3.a": "每周简报每月 0.99 credits 起，隔日简报（周一、周三、周五发送）每月 1.99 credits，含 AI智能体数据接口的进阶每日简报每月 2.49 credits。Credits 以 US$10 和 US$20 点数包出售——详见定价页面。",
    "welcome.subscriptionPlans": "Portfonia 向订阅用户发送定期简报。有三种套餐：每周简报（每月 0.99 credits）、隔日简报（每月 1.99 credits，周一、周三、周五发送）和进阶每日简报（每月 2.49 credits，工作日发送，含 AI智能体数据接口）。",
    "legal.pricing.sections.0.body.1": "隔日简报——每月 1.99 credits。每周三封个性化简报邮件（周一、周三、周五）。",
    "home.how.cards.2.body": "按每周、隔日或每日推送，紧扣你的真实持仓——包含价格异动、技术面位置，以及宏观与财报的前瞻日历。",
    "legal.pricing.sections.0.body.3": "每周与隔日两档的区别仅在于发送频率。每日为进阶套餐：除工作日简报外，还允许您的 AI智能体读取逐日持仓快照和与持仓相关的情报。所有套餐都可以让 AI智能体读取历史报告。"
  },
  "zh-Hant": {
    "menu.reports": "報告中心",
    "reports.title": "報告中心",
    "profile.reportScheduleOptions.everyOtherDay": "隔日",
    "reports.types.everyOtherDay": "隔日",
    "profile.subscriptionTitle.mwf": "隔日簡報",
    "home.faq.items.3.a": "每週簡報每月 0.99 credits 起，隔日簡報（週一、週三、週五寄送）每月 1.99 credits，含 AI 智能體資料介面的進階每日簡報每月 2.49 credits。Credits 以 US$10 與 US$20 點數包出售——詳見定價頁面。",
    "welcome.subscriptionPlans": "Portfonia 向訂閱使用者寄送定期簡報。有三種方案：每週簡報（每月 0.99 credits）、隔日簡報（每月 1.99 credits，週一、週三、週五寄送）和進階每日簡報（每月 2.49 credits，工作日寄送，含 AI 智能體資料介面）。",
    "legal.pricing.sections.0.body.1": "隔日簡報——每月 1.99 credits。每週三封個人化簡報郵件（週一、週三、週五）。",
    "home.how.cards.2.body": "按每週、隔日或每日推送，緊扣你的實際持倉——包含價格異動、技術面位置，以及總經與財報的前瞻日曆。",
    "legal.pricing.sections.0.body.3": "每週與隔日兩種方案的差別僅在於寄送頻率。每日為進階方案：除工作日簡報外，還允許您的 AI 智能體讀取逐日持倉快照和與持倉相關的情報。所有方案都可以讓 AI 智能體讀取歷史報告。"
  }
};
const english = {
  "menu.reports": "Reports",
  "reports.title": "Reports",
  "profile.reportScheduleOptions.everyOtherDay": "Mon/Wed/Fri",
  "reports.types.everyOtherDay": "Mon/Wed/Fri",
  "profile.subscriptionTitle.mwf": "Mon/Wed/Fri briefing",
  "home.faq.items.3.a": "Plans start at 0.99 credits per month for a weekly briefing, 1.99 for Monday / Wednesday / Friday briefings, and 2.49 for the Advanced Daily plan with AI Agent data access. Credits are sold in US$10 and US$20 packs \u2014 see Pricing.",
  "welcome.subscriptionPlans": "Portfonia sends scheduled briefings to subscribers. There are three plans: Weekly (0.99 credits per month), Mon/Wed/Fri (1.99 credits per month) and the Advanced Daily plan (2.49 credits per month, weekdays, with AI Agent data access).",
  "legal.pricing.sections.0.body.1": "Mon / Wed / Fri briefing \u2014 1.99 credits per month. Three personalized briefing emails per week (Monday, Wednesday, Friday).",
  "home.how.cards.2.body": "Mon / Wed / Fri, tied to your actual holdings \u2014 price anomalies, technical position, and a forward calendar of macro and earnings events.",
  "legal.pricing.sections.0.body.3": "Weekly and Mon / Wed / Fri differ only in how often briefings are sent. Daily is the Advanced plan: besides weekday briefings, it lets your AI agent read your daily holding snapshots and holding-related intelligence. Report history is available to AI agents on every plan."
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
