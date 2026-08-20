/** One shared client instance for the whole application. */
import { ApiClient } from "./client";

export const api = new ApiClient();

export * from "./client";
export * from "./types";
