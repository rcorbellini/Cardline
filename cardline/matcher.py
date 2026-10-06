"""Identificação de cartas num frame: SIFT + FLANN + votação + homografia (RANSAC)."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .index import RefIndex, root_sift

FRAME_FEATURES = 4000
RATIO = 0.8  # teste de Lowe (contra o melhor vizinho de *outra* arte)
MIN_VOTES = 6
RANSAC_PX = 6.0


@dataclass
class Match:
    card: int  # índice no RefIndex
    inliers: int
    votes: int
    H: np.ndarray  # homografia imagem oficial → frame
    quad: np.ndarray  # (4, 2) cantos da carta no frame: sup-esq, sup-dir, inf-dir, inf-esq


class Matcher:
    def __init__(self, ref: RefIndex, nfeatures: int = FRAME_FEATURES):
        self.ref = ref
        self.sift = cv2.SIFT_create(nfeatures=nfeatures)
        self.flann = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 4}, {"checks": 64})
        self.flann.add([ref.desc])
        self.flann.train()

    def match(self, gray: np.ndarray, candidates: int = 4) -> list[Match]:
        """Cartas verificadas no frame, da mais visível (mais inliers) para a menos."""
        kps, desc = self.sift.detectAndCompute(gray, None)
        if desc is None or len(kps) < MIN_VOTES:
            return []
        ref = self.ref
        good_q, good_t = [], []
        for nbrs in self.flann.knnMatch(root_sift(desc), k=3):
            if not nbrs:
                continue
            best = nbrs[0]
            group = ref.groups[ref.owner[best.trainIdx]]
            other = next((n for n in nbrs[1:] if ref.groups[ref.owner[n.trainIdx]] != group), None)
            if other is None or best.distance < RATIO * other.distance:
                good_q.append(best.queryIdx)
                good_t.append(best.trainIdx)
        if len(good_t) < MIN_VOTES:
            return []
        good_q, good_t = np.array(good_q), np.array(good_t)
        owners = ref.owner[good_t]
        votes = np.bincount(owners, minlength=len(ref.card_ids))
        frame_pts = np.float32([k.pt for k in kps])
        h, w = gray.shape
        out = []
        for card in np.argsort(-votes)[:candidates]:
            if votes[card] < MIN_VOTES:
                break
            sel = owners == card
            src, dst = ref.kp[good_t[sel]], frame_pts[good_q[sel]]
            H, mask = cv2.findHomography(src, dst, cv2.RANSAC, RANSAC_PX, maxIters=2000, confidence=0.995)
            if H is None:
                continue
            cw, ch = ref.sizes[card]
            corners = np.float32([[0, 0], [cw, 0], [cw, ch], [0, ch]]).reshape(-1, 1, 2)
            quad = cv2.perspectiveTransform(corners, H).reshape(4, 2)
            if not _plausible(quad, w * h):
                continue
            out.append(Match(int(card), int(mask.sum()), int(votes[card]), H, quad))
        out.sort(key=lambda m: -m.inliers)
        return out


def _plausible(quad: np.ndarray, frame_area: float) -> bool:
    """Descarta homografias degeneradas: a carta precisa ser um quadrilátero convexo e razoável."""
    if not cv2.isContourConvex(quad.astype(np.float32).reshape(-1, 1, 2)):
        return False
    area = cv2.contourArea(quad.astype(np.float32))
    if not 0.02 * frame_area < area < 1.5 * frame_area:
        return False
    sides = np.linalg.norm(quad - np.roll(quad, -1, axis=0), axis=1)
    top, right, bottom, left = sides
    return 0.6 < top / bottom < 1.6 and 0.6 < left / right < 1.6 and 0.45 < (top + bottom) / (left + right) < 1.1
