import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/**
 * Guardrails that would be easy to breach later without noticing.
 *
 * These read the frontend's own source. They are cheap, and each one protects
 * a boundary that only shows up as a security or correctness problem long
 * after the mistake is made.
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

/**
 * Strip comments so a guardrail tests the CODE, not the prose.
 *
 * These files explain the rules they follow — `client.ts` says in a comment
 * that it deliberately never touches `localStorage`. Scanning raw text makes
 * that honest explanation trip the very rule it documents, which would push
 * future authors to stop writing the explanation.
 *
 * String and template literals are preserved, so a real `"localStorage"` in
 * code is still caught.
 */
function stripComments(source: string): string {
  let out = "";
  let index = 0;
  let quote: string | null = null;

  while (index < source.length) {
    const char = source[index];
    const next = source[index + 1];

    if (quote) {
      if (char === "\\") {
        out += char + (next ?? "");
        index += 2;
        continue;
      }

      if (char === quote) quote = null;

      out += char;
      index += 1;
      continue;
    }

    if (char === '"' || char === "'" || char === "`") {
      quote = char;
      out += char;
      index += 1;
      continue;
    }

    if (char === "/" && next === "/") {
      while (index < source.length && source[index] !== "\n") index += 1;
      continue;
    }

    if (char === "/" && next === "*") {
      index += 2;
      while (index < source.length && !(source[index] === "*" && source[index + 1] === "/")) {
        index += 1;
      }
      index += 2;
      continue;
    }

    out += char;
    index += 1;
  }

  return out;
}

const FILES = sourceFiles(SRC).map((path) => ({
  path,
  text: stripComments(readFileSync(path, "utf8")),
}));

describe("the frontend is a thin client", () => {
  it("imports no database or vector-store client", () => {
    // The browser must reach Phase 13 over HTTP and nothing else.
    //
    // These match IMPORTS and CONNECTION constructs, not bare words: the
    // search page legitimately matches /qdrant|vector/ against a backend error
    // message to render the "vector search unavailable" state, and banning the
    // substring would forbid reading the API's own error.
    const banned: [RegExp, string][] = [
      [/from\s+["'][^"']*qdrant[^"']*["']/i, "a Qdrant client import"],
      [/require\(\s*["'][^"']*qdrant[^"']*["']\s*\)/i, "a Qdrant client require"],
      [/from\s+["'](pg|mysql2?|mongodb|mssql|tedious)["']/i, "a database driver import"],
      [/new\s+(MongoClient|QdrantClient|Client)\s*\(/, "a database client construction"],
      [/\b(postgresql|mysql|mongodb):\/\//i, "a database connection URI"],
      [/\bpsycopg|\bsqlalchemy/i, "a Python database library"],
    ];

    for (const { path, text } of FILES) {
      for (const [pattern, description] of banned) {
        expect(pattern.test(text), `${path} contains ${description}`).toBe(false);
      }
    }
  });

  it("issues no SQL", () => {
    for (const { path, text } of FILES) {
      expect(/\bSELECT\s+.+\s+FROM\b/i.test(text), `${path} contains SQL`).toBe(
        false,
      );
    }
  });

  it("routes every request through the shared client", () => {
    // A stray fetch() in a component would bypass error handling and the
    // configured base URL.
    for (const { path, text } of FILES) {
      if (path.endsWith("client.ts")) continue;

      expect(/\bfetch\s*\(/.test(text), `${path} calls fetch directly`).toBe(
        false,
      );
    }
  });

  it("hard-codes no backend URL outside the client", () => {
    for (const { path, text } of FILES) {
      if (path.endsWith("client.ts")) continue;

      expect(
        /https?:\/\/(127\.0\.0\.1|localhost)/.test(text),
        `${path} hard-codes a backend URL`,
      ).toBe(false);
    }
  });
});

describe("secrets never persist in the browser", () => {
  it("writes nothing to localStorage or sessionStorage", () => {
    // Anything stored there survives the tab and is readable by any script on
    // the page, which is exactly wrong for a credential.
    for (const { path, text } of FILES) {
      expect(text.includes("localStorage"), `${path} uses localStorage`).toBe(
        false,
      );
      expect(
        text.includes("sessionStorage"),
        `${path} uses sessionStorage`,
      ).toBe(false);
    }
  });

  it("never logs a password field", () => {
    for (const { path, text } of FILES) {
      expect(
        /console\.(log|info|warn|error)\([^)]*password/i.test(text),
        `${path} logs a password`,
      ).toBe(false);
    }
  });

  it("keeps the password out of any URL", () => {
    for (const { path, text } of FILES) {
      expect(
        /[?&]password=/.test(text),
        `${path} puts a password in a query string`,
      ).toBe(false);
    }
  });
});

describe("no vector ever reaches the interface", () => {
  it("renders no embedding vector field", () => {
    // The backend deliberately omits vectors from every response; the UI must
    // not add a field that would surface one if that ever changed.
    for (const { path, text } of FILES) {
      expect(
        /hit\.vector|\.embedding_vector|result\.vector\b/.test(text),
        `${path} renders a vector`,
      ).toBe(false);
    }
  });
});

describe("the frontend is only an upload screen", () => {
  it("calls no endpoint other than the two upload routes", () => {
    // The backend still implements discovery, mapping, jobs, search and the
    // rest; this frontend deliberately surfaces none of it. A stray path here
    // would quietly reintroduce a removed feature.
    const removed = [
      "/v1/sources",
      "/v1/schemas",
      "/v1/mappings",
      "/v1/jobs",
      "/v1/search",
      "/v1/records",
      "/v1/health",
      "/v1/capabilities",
      "/v1/api-specs",
    ];

    for (const { path, text } of FILES) {
      for (const route of removed) {
        expect(text.includes(route), `${path} still calls ${route}`).toBe(false);
      }
    }
  });

  it("contains no routing and no navigation", () => {
    for (const { path, text } of FILES) {
      expect(
        text.includes("react-router"),
        `${path} still imports a router`,
      ).toBe(false);
      expect(/<NavLink|<Routes|<Route/.test(text), `${path} still routes`).toBe(
        false,
      );
    }
  });

  it("has no page component other than the upload screen", () => {
    // Split on both separators: these are absolute Windows paths here.
    const pages = sourceFiles(join(SRC, "pages")).map((p) =>
      p.split(/[\\/]/).pop(),
    );

    expect(pages.sort()).toEqual(["Upload.tsx"]);
  });

  it("uses only the two upload client methods", () => {
    const client = FILES.find(({ path }) => path.endsWith("client.ts"));
    const removedMethods = [
      "listSources",
      "createSource",
      "discoverSource",
      "suggestMapping",
      "createJob",
      "getJob",
      "search(",
      "getRecord",
      "capabilities(",
      "uploadOpenApi",
      "uploadPostman",
    ];

    for (const method of removedMethods) {
      expect(
        client!.text.includes(method),
        `client.ts still exposes ${method}`,
      ).toBe(false);
    }

    expect(client!.text.includes("uploadCsv")).toBe(true);
    expect(client!.text.includes("uploadDocument")).toBe(true);
  });
});

describe("no fabricated metrics", () => {
  it("hard-codes no accuracy, recall or cost claim", () => {
    // Research numbers belong to the benchmark artifact and the report, never
    // to a dashboard card.
    const claims = [
      /\b9[0-9](\.\d+)?%\s*(accuracy|recall|precision)/i,
      /\b\d+x\s*(cheaper|faster)\b/i,
      /recall@\d\s*=\s*0?\.\d+/i,
    ];

    for (const { path, text } of FILES) {
      for (const claim of claims) {
        expect(claim.test(text), `${path} states a hard-coded metric`).toBe(
          false,
        );
      }
    }
  });
});
