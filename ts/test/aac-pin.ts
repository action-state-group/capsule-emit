/**
 * The agent-action-capsule commit whose vectors and data these tests replay.
 *
 * CI checks out exactly this commit (ci.yml and publish.yml read it from the
 * line below), and test/aac-pin.test.ts refuses an AAC checkout at any other
 * commit, so a change upstream can't turn this repository red, or green,
 * without a commit here that says so.
 *
 * To bump: check the new AAC commit out next to this repository (or point
 * AAC_ROOT at a checkout of it), set AAC_COMMIT below to its full hash, run
 * `npm test`, fix what fails, and list in the pull request the AAC changes the
 * bump takes in.
 */
export const AAC_COMMIT = "36d6770cf1856ed9043d98782275a14ce221fdde";
