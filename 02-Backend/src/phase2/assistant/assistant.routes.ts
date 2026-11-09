import { Router } from 'express';

import { type ProjectRequest, sendError } from '../shared/http';

const MAX_QUESTION_LENGTH = 2000;
const WINDOW_MS = 60_000;
const MAX_QUESTIONS_PER_WINDOW = 20;
const DEFAULT_TIMEOUT_MS = 60_000;

type Options = {
  fetchImpl?: typeof fetch;
  env?: NodeJS.ProcessEnv;
  now?: () => number;
};

/** POST /api/assistant: authenticates in Express, then asks the private Python chatbot service. */
export function createAssistantRouter({ fetchImpl = fetch, env = process.env, now = Date.now }: Options = {}) {
  const router = Router();
  const recent = new Map<number, number[]>();

  function allowQuestion(userId: number) {
    const cutoff = now() - WINDOW_MS;
    const times = (recent.get(userId) ?? []).filter((time) => time > cutoff);
    if (times.length >= MAX_QUESTIONS_PER_WINDOW) {
      recent.set(userId, times);
      return false;
    }
    times.push(now());
    recent.set(userId, times);
    return true;
  }

  router.post('/', async (req, res) => {
    const { projectUser: user } = req as ProjectRequest;
    const question = req.body?.question;

    if (typeof question !== 'string' || !question.trim() || question.length > MAX_QUESTION_LENGTH) {
      return sendError(res, 400, 'INVALID_QUESTION', `A question of 1 to ${MAX_QUESTION_LENGTH} characters is required`);
    }
    if (!allowQuestion(user.id)) {
      res.set('Retry-After', '60');
      return sendError(res, 429, 'ASSISTANT_RATE_LIMITED', 'Too many questions; try again in a minute');
    }

    const baseUrl = env.CHATBOT_URL;
    const internalKey = env.CHATBOT_INTERNAL_KEY;
    if (!baseUrl || !internalKey) {
      return sendError(res, 503, 'ASSISTANT_UNAVAILABLE', 'The assistant is not configured');
    }

    let upstream: Response;
    try {
      upstream = await fetchImpl(`${baseUrl.replace(/\/$/, '')}/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: req.get('authorization') ?? '',
          'X-Internal-Key': internalKey
        },
        body: JSON.stringify({ question: question.trim() }),
        signal: AbortSignal.timeout(Number(env.CHATBOT_TIMEOUT_MS) || DEFAULT_TIMEOUT_MS)
      });
    } catch (error) {
      if ((error as Error).name === 'TimeoutError') {
        return sendError(res, 504, 'ASSISTANT_TIMEOUT', 'The assistant took too long to respond');
      }
      return sendError(res, 503, 'ASSISTANT_UNAVAILABLE', 'The assistant is temporarily unavailable');
    }

    const body = (await upstream.json().catch(() => null)) as { answer?: unknown } | null;
    if (upstream.ok && typeof body?.answer === 'string') {
      return res.status(200).json({ data: { answer: body.answer } });
    }
    if (upstream.status === 401) {
      return sendError(res, 401, 'UNAUTHENTICATED', 'Authentication is required');
    }
    return sendError(res, 503, 'ASSISTANT_UNAVAILABLE', 'The assistant is temporarily unavailable');
  });

  return router;
}
