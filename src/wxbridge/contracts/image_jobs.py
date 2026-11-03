"""Distinct image proposal and queued-job records; JSON belongs at storage boundaries."""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TypedDict
import re


class ImageArtifact(TypedDict):
    file: str
    mime: str
    bytes: int
    sha256: str
    md5: str
    width: int
    height: int


@dataclass(frozen=True)
class QuotedImageInput:
    data: bytes = field(repr=False)
    mime: str
    trigger_index: int
    depth: int
    source: str


@dataclass(frozen=True)
class ImageProposal:
    prompt: str = field(repr=False)
    reference_image: QuotedImageInput | None = field(default=None, repr=False)


@dataclass(frozen=True)
class QueuedImageJob:
    model: str
    prompt: str = field(repr=False)
    options: dict = field(repr=False)
    source_image: ImageArtifact | None = field(default=None, repr=False)

    def to_document(self):
        value = dict(model=self.model, prompt=self.prompt, options=deepcopy(self.options))
        if self.source_image is not None:value['source_image'] = deepcopy(self.source_image)
        return value

    @classmethod
    def from_document(cls, value):
        if (not isinstance(value, dict) or set(value) not in
                ({'model','prompt','options'}, {'model','prompt','options','source_image'})
                or not isinstance(value['model'],str) or not value['model'].strip()
                or not isinstance(value['prompt'],str) or not 1 <= len(value['prompt'].strip()) <= 4000
                or not isinstance(value['options'],dict)):
            raise ValueError('invalid_queued_image_job')
        source = value.get('source_image')
        if source is not None:
            fields = {'file','mime','bytes','sha256','md5','width','height'}
            if (not isinstance(source,dict) or set(source)!=fields
                    or not isinstance(source['file'],str) or not re.fullmatch(r'[a-f0-9]{32}\.(png|jpg)',source['file'])
                    or source['mime'] not in ('image/png','image/jpeg')
                    or any(type(source[k]) is not int or source[k]<1 for k in ('bytes','width','height'))
                    or source['bytes']>8*1024*1024
                    or any(not isinstance(source[k],str) or not re.fullmatch(pattern,source[k])
                           for k,pattern in (('md5',r'[a-f0-9]{32}'),('sha256',r'[a-f0-9]{64}')))):
                raise ValueError('invalid_image_artifact_reference')
        return cls(value['model'],value['prompt'],deepcopy(value['options']),deepcopy(source))
