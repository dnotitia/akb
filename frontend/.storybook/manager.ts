import { addons } from "storybook/manager-api";
import { create } from "storybook/theming/create";

addons.setConfig({
  theme: create({
    base: "light",
    brandTitle: "AKB Storybook",
    brandUrl: "/",
    colorPrimary: "#004059",
    colorSecondary: "#e55e2c",
    appBg: "#f6f7f9",
    appContentBg: "#ffffff",
    appBorderColor: "#dfe3e8",
    textColor: "#1d1d1f",
    textMutedColor: "#5e6068",
    barBg: "#ffffff",
    barTextColor: "#5e6068",
    barSelectedColor: "#004059",
    inputBg: "#ffffff",
    inputBorder: "#dfe3e8",
    inputTextColor: "#1d1d1f",
    inputBorderRadius: 12,
  }),
});
