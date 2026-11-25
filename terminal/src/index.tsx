import { render } from "ink";
import { login } from "../../frontend/cli/auth/deviceFlow";
import { loadConfig, saveConfig } from "../../frontend/cli/auth/store";
import { openSavedThread } from "./session";
import { App } from "./app";
import { terminalText } from "./adapter";

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--help")) {
    console.log(
      "Usage: ./nous [login | --new]\n  login  sign in through the browser\n  --new  start a fresh thread",
    );
    return;
  }
  if (args[0] === "login" && args.length === 1) {
    await login();
    return;
  }
  if (args.some((arg) => arg !== "--new"))
    throw new Error("Usage: ./nous [login | --new]");
  if (!process.stdin.isTTY || !process.stdout.isTTY)
    throw new Error("Ink requires an interactive terminal.");
  const config = loadConfig();
  if (!config?.token || !(Date.parse(config.expires_at) > Date.now())) {
    throw new Error("Login required. Run ./nous login first.");
  }
  if (args.includes("--new")) {
    config.thread_id = null;
    saveConfig(config);
  }
  const {
    messages: initialMessages,
    history: initialHistory,
    notice: initialNotice,
  } = await openSavedThread(config.thread_id);
  const instance = render(
    <App
      initialMessages={initialMessages}
      initialHistory={initialHistory}
      initialNotice={initialNotice}
    />,
    { exitOnCtrlC: false, interactive: true },
  );
  const result = await instance.waitUntilExit();
  if (typeof result === "string") console.log(terminalText(result));
}

main().catch((error: unknown) => {
  console.error(
    terminalText(error instanceof Error ? error.message : String(error)),
  );
  process.exitCode = 1;
});
