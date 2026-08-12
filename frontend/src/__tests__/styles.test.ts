import { describe, expect, it } from "vitest";
import styles from "../styles.css?raw";

describe("keyboard and mobile styling", () => {
  it("keeps focus visible for visually hidden upload and radio controls", () => {
    expect(styles).toMatch(/\.drop-zone:focus-within/);
    expect(styles).toMatch(/\.preset:focus-within/);
  });

  it("keeps private Tailscale status visible on narrow phones", () => {
    const narrowPhoneRules = styles.split("@media (max-width: 400px)")[1] ?? "";
    expect(narrowPhoneRules).not.toMatch(/\.topbar-status\s*\{\s*display:\s*none/);
  });
});
