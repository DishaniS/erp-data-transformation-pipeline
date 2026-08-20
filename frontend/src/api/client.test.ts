import { describe, expect, it, vi } from "vitest";

import {
  ApiClient,
  ApiError,
  DEFAULT_BASE_URL,
  NetworkError,
  classifyUpload,
  isAcceptedBy,
  resolveBaseUrl,
  uploadPathFor,
} from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("base URL", () => {
  it("falls back to loopback when nothing is configured", () => {
    expect(resolveBaseUrl("")).toBe(DEFAULT_BASE_URL);
  });

  it("strips a trailing slash so paths do not double up", () => {
    expect(resolveBaseUrl("http://api.test:9000/")).toBe("http://api.test:9000");
  });

  it("builds absolute URLs", () => {
    const client = new ApiClient({ baseUrl: "http://api.test:9000/" });

    expect(client.url("/v1/files/csv")).toBe("http://api.test:9000/v1/files/csv");
    // A path without a leading slash must not concatenate into the host.
    expect(client.url("v1/files/documents")).toBe(
      "http://api.test:9000/v1/files/documents",
    );
  });
});

describe("upload routing", () => {
  it("sends each file kind to the endpoint the backend expects", () => {
    expect(classifyUpload("invoice.csv")).toBe("csv");
    expect(classifyUpload("purchase_order.pdf")).toBe("document");
    expect(classifyUpload("scan.PNG")).toBe("document");
    expect(classifyUpload("receipt.jpeg")).toBe("document");
    expect(classifyUpload("photo.jpg")).toBe("document");
  });

  it("maps each kind to the documented path", () => {
    expect(uploadPathFor("csv")).toBe("/v1/files/csv");
    expect(uploadPathFor("document")).toBe("/v1/files/documents");
  });

  it("refuses a type neither box accepts", () => {
    // API specifications are no longer part of this frontend.
    expect(classifyUpload("vendor.yaml")).toBeNull();
    expect(classifyUpload("vendor.json")).toBeNull();
    expect(classifyUpload("archive.zip")).toBeNull();
    expect(classifyUpload("noextension")).toBeNull();
  });

  it("keeps each box to its own file types", () => {
    expect(isAcceptedBy("csv", "invoice.csv")).toBe(true);
    // A PDF must never be posted to the CSV endpoint, or the reverse.
    expect(isAcceptedBy("csv", "invoice.pdf")).toBe(false);
    expect(isAcceptedBy("document", "invoice.pdf")).toBe(true);
    expect(isAcceptedBy("document", "invoice.csv")).toBe(false);
  });

  it("posts a CSV to /v1/files/csv as multipart", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ upload_id: "u1" }));
    const client = new ApiClient({
      baseUrl: "http://api.test",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await client.uploadCsv(new File(["a,b\n1,2\n"], "invoice.csv"));

    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("http://api.test/v1/files/csv");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("file")).toBeInstanceOf(File);
    // The browser must set the multipart boundary itself.
    expect(init.headers).toBeUndefined();
  });

  it("posts a document to /v1/files/documents", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ upload_id: "u2" }));
    const client = new ApiClient({
      baseUrl: "http://api.test",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await client.uploadDocument(new File(["%PDF-"], "order.pdf"));

    expect(fetchImpl.mock.calls[0][0]).toBe("http://api.test/v1/files/documents");
  });
});

describe("errors", () => {
  it("surfaces the backend's structured code and message", async () => {
    const client = new ApiClient({
      baseUrl: "http://api.test",
      fetchImpl: vi.fn().mockResolvedValue(
        jsonResponse(
          {
            success: false,
            error: {
              code: "UNSUPPORTED_UPLOAD",
              message: ".zip is not accepted by this endpoint",
              request_id: "abc123",
            },
          },
          415,
        ),
      ) as unknown as typeof fetch,
    });

    await expect(
      client.uploadCsv(new File(["x"], "bad.csv")),
    ).rejects.toMatchObject({
      code: "UNSUPPORTED_UPLOAD",
      status: 415,
      requestId: "abc123",
    });
  });

  it("reports an oversized upload with the backend's own code", async () => {
    const client = new ApiClient({
      baseUrl: "http://api.test",
      fetchImpl: vi.fn().mockResolvedValue(
        jsonResponse(
          {
            success: false,
            error: { code: "UPLOAD_TOO_LARGE", message: "too large" },
          },
          413,
        ),
      ) as unknown as typeof fetch,
    });

    await expect(
      client.uploadCsv(new File(["x"], "big.csv")),
    ).rejects.toMatchObject({ code: "UPLOAD_TOO_LARGE", status: 413 });
  });

  it("replaces an unreachable backend with an actionable message", async () => {
    const client = new ApiClient({
      baseUrl: "http://api.test",
      fetchImpl: vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) as
        unknown as typeof fetch,
    });

    await expect(
      client.uploadCsv(new File(["x"], "a.csv")),
    ).rejects.toBeInstanceOf(NetworkError);
    await expect(
      client.uploadCsv(new File(["x"], "a.csv")),
    ).rejects.toThrow(/ERP_API_CORS_ORIGINS/);
  });

  it("still fails cleanly when the body is not JSON", async () => {
    const client = new ApiClient({
      baseUrl: "http://api.test",
      fetchImpl: vi
        .fn()
        .mockResolvedValue(new Response("upstream failure", { status: 502 })) as
        unknown as typeof fetch,
    });

    await expect(
      client.uploadCsv(new File(["x"], "a.csv")),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
