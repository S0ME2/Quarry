import js from "@eslint/js";
import ts from "typescript-eslint";
import hooks from "eslint-plugin-react-hooks";
export default ts.config(js.configs.recommended, ...ts.configs.recommended, {
  files: ["**/*.{ts,tsx}"],
  plugins: { "react-hooks": hooks },
  rules: {
    "react-hooks/rules-of-hooks": "error",
    "react-hooks/exhaustive-deps": "error",
  },
  languageOptions: {
    globals: {
      window: "readonly",
      document: "readonly",
      fetch: "readonly",
      console: "readonly",
      AbortController: "readonly",
      URLSearchParams: "readonly",
      FormData: "readonly",
      crypto: "readonly",
      setTimeout: "readonly",
      clearTimeout: "readonly",
    },
  },
});
