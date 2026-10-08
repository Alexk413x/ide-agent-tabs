import { history } from '../src/messaging/history.js';
import { peekUnread, sendMessage, takeBatch, type Message, type MessageFilter } from '../src/messaging/store.js';

export { newMessageId, type Message } from '../src/messaging/store.js';

export async function deliverTo(home: string, message: Message, now = Date.now()): Promise<string> {
  const { from, to, text, replyTo } = message;
  const sent = await sendMessage(home, { from, to, text, ...(replyTo !== undefined ? { replyTo } : {}) }, now);
  if (sent.duplicate) throw new Error(`deliverTo: the store took "${text.slice(0, 40)}" as a duplicate; give each test message its own text`);
  return sent.id;
}

export const unread = (home: string, id: string, now = Date.now()): Promise<Message[]> => peekUnread(home, id, now);

export const take = async (home: string, id: string, filter: MessageFilter = {}, limit = Infinity): Promise<Message[]> =>
  (await takeBatch(home, id, { filter, count: limit })).messages;

export async function readBy(home: string, id: string): Promise<string[]> {
  const items = await history(home, { id, names: [] });
  return items.filter((m) => m.direction === 'received' && m.status === 'read').map((m) => m.id);
}
