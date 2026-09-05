import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/**
 * The UI must never show business data it invented.
 *
 * A demo row that looks like a real employee is worse than an empty screen:
 * a reader cannot tell the difference, and "the system found EMP-0001" is a
 * claim about live data. Every business value rendered here has to arrive
 * from an API response.
 *
 * These read the frontend's own source, matching the convention already used
 * by `safety.test.ts` — this project has no React test renderer, and a
 * source-level guardrail catches the mistake at the point it would be made.
 */

const SRC = join(process.cwd(), "src");

function sourceFiles(directory: string): string[] {
  const found: string[] = [];

  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);

    if (statSync(path).isDirectory()) {
      found.push(...sourceFiles(path));
    } else if (/\.(ts|tsx)$/.test(entry) && !entry.endsWith(".test.ts")) {
      found.push(path);
    }
  }

  return found;
}

/** Strip comments so a guardrail tests the CODE, not the prose explaining it. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}

describe("the UI renders real data or nothing", () => {
  it("contains no fabricated business records", () => {
    // Identifiers and names that would be indistinguishable from live ERP
    // data if they were ever rendered.
    const fabricated = [
      /EMP-?\d{3,}/,
      /MC-\d{4}-\d+/,
      /John Doe/i,
      /Jane Doe/i,
      /Demo Medical/i,
      /medical_claim/,
      /acme_erp/i,
      /legacy_erp_pg/,
    ];

    for (const path of sourceFiles(SRC)) {
      const text = code(readFileSync(path, "utf8"));

      for (const pattern of fabricated) {
        expect(
          pattern.test(text),
          `${path} contains fabricated business data matching ${pattern}`,
        ).toBe(false);
      }
    }
  });

  it("declares no seeded rows, records or results", () => {
    // A literal array of business objects is how demo data gets in. The
    // component holds ONE piece of state and it is a status, not a dataset.
    const seeded = [
      /const\s+(sample|demo|dummy|fake|mock|seed)[A-Za-z]*\s*(:|=)/i,
      /\b(sampleRows|demoRows|fakeRows|mockData|placeholderRows)\b/,
    ];

    for (const path of sourceFiles(SRC)) {
      const text = code(readFileSync(path, "utf8"));

      for (const pattern of seeded) {
        expect(
          pattern.test(text),
          `${path} declares seeded data matching ${pattern}`,
        ).toBe(false);
      }
    }
  });

  it("renders nothing in its resting state, rather than example content", () => {
    // The status union has an explicit `idle` member with no payload, and the
    // render block has no branch for it. An idle screen therefore shows the
    // control and no results — which is the correct empty state.
    const upload = code(readFileSync(join(SRC, "pages", "Upload.tsx"), "utf8"));

    expect(upload).toContain('phase: "idle"');
    expect(upload).not.toMatch(/status\.phase === "idle" &&/);

    // Every rendered business value is read off the status object, which is
    // only ever populated from an awaited API call.
    expect(upload).toContain("status.detail");
    expect(upload).toContain("status.filename");
  });

  it("only ever fills the status from an API response", () => {
    const upload = code(readFileSync(join(SRC, "pages", "Upload.tsx"), "utf8"));

    // The one place a "done" status is produced must be fed by the awaited
    // upload result, never by a literal.
    expect(upload).toMatch(/detail:\s*await\s+onUpload\(file\)/);
    expect(upload).toMatch(/await\s+api\.uploadCsv\(file\)/);
    expect(upload).toMatch(/await\s+api\.uploadDocument\(file\)/);
  });

  it("keeps input hints as hints - never as submitted values", () => {
    // Placeholder text guiding a user is fine. What is not fine is a default
    // VALUE that gets sent if the user types nothing.
    for (const path of sourceFiles(SRC)) {
      const text = code(readFileSync(path, "utf8"));

      // `defaultValue=` on an input would submit without the user's intent.
      expect(
        /defaultValue\s*=\s*["'{]/.test(text),
        `${path} sets a defaultValue that could be submitted as real input`,
      ).toBe(false);
    }
  });
});
