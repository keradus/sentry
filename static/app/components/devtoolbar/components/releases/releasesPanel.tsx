import {Fragment} from 'react';
import {css} from '@emotion/react';

import AnalyticsProvider from 'sentry/components/devtoolbar/components/analyticsProvider';
import ReleaseIsssues from 'sentry/components/devtoolbar/components/releases/releaseIssues';
import useReleaseSessions from 'sentry/components/devtoolbar/components/releases/useReleaseSessions';
import useToolbarRelease from 'sentry/components/devtoolbar/components/releases/useToolbarRelease';
import SentryAppLink from 'sentry/components/devtoolbar/components/sentryAppLink';
import {listItemPlaceholderWrapperCss} from 'sentry/components/devtoolbar/styles/listItem';
import {
  infoHeaderCss,
  subtextCss,
} from 'sentry/components/devtoolbar/styles/releasesPanel';
import {
  resetFlexColumnCss,
  resetFlexRowCss,
} from 'sentry/components/devtoolbar/styles/reset';
import type {ApiResult} from 'sentry/components/devtoolbar/types';
import EmptyStateWarning from 'sentry/components/emptyStateWarning';
import ProjectBadge from 'sentry/components/idBadge/projectBadge';
import PanelItem from 'sentry/components/panels/panelItem';
import Placeholder from 'sentry/components/placeholder';
import TimeSince from 'sentry/components/timeSince';
import {IconArrow} from 'sentry/icons/iconArrow';
import type {SessionApiResponse} from 'sentry/types/organization';
import type {PlatformKey} from 'sentry/types/project';
import type {Release} from 'sentry/types/release';
import {defined} from 'sentry/utils';
import {formatVersion} from 'sentry/utils/versions/formatVersion';
import {
  Change,
  type ReleaseComparisonRow,
} from 'sentry/views/releases/detail/overview/releaseComparisonChart';
import {
  ReleaseInfoHeader,
  ReleaseInfoSubheader,
  VersionWrapper,
} from 'sentry/views/releases/list/releaseCard';
import ReleaseCardCommits from 'sentry/views/releases/list/releaseCard/releaseCardCommits';

import useConfiguration from '../../hooks/useConfiguration';
import {panelInsetContentCss, panelSectionCss} from '../../styles/panel';
import {smallCss} from '../../styles/typography';
import PanelLayout from '../panelLayout';

const summaryPlaceholderHeight = '65px';
const crashComparisonPlaceholderHeight = '61px';
const issueListPlaceholderHeight = '320px';

function getCrashFreeRate(data: ApiResult<SessionApiResponse>): number {
  // if `crash_free_rate(session)` is undefined
  // (sometimes the case for brand new releases),
  // assume it is 100%.
  // round to 2 decimal points
  return parseFloat(
    ((data?.json.groups[0].totals['crash_free_rate(session)'] ?? 1) * 100).toFixed(2)
  );
}

function getDiff(
  diff: string,
  diffColor: ReleaseComparisonRow['diffColor'],
  diffDirection: 'up' | 'down' | undefined
) {
  return (
    <Change
      color={defined(diffColor) ? diffColor : 'black'}
      css={[resetFlexRowCss, {alignItems: 'center', gap: 'var(--space25)'}]}
    >
      {diff}
      {defined(diffDirection) ? <IconArrow direction={diffDirection} size="xs" /> : null}
    </Change>
  );
}

function ReleaseSummary({orgSlug, release}: {orgSlug: string; release: Release}) {
  return (
    <PanelItem
      css={{width: '100%', alignItems: 'flex-start', padding: 'var(--space150)'}}
    >
      <ReleaseInfoHeader css={infoHeaderCss}>
        <SentryAppLink
          to={{
            url: `/organizations/${orgSlug}/releases/${encodeURIComponent(release.version)}/`,
            query: {project: release.projects[0].id},
          }}
        >
          <VersionWrapper>{formatVersion(release.version)}</VersionWrapper>
        </SentryAppLink>
        {release.commitCount > 0 && (
          <ReleaseCardCommits release={release} withHeading={false} />
        )}
      </ReleaseInfoHeader>
      <ReleaseInfoSubheader
        css={[resetFlexColumnCss, subtextCss, {alignItems: 'flex-start'}]}
      >
        <span css={[resetFlexRowCss, {gap: 'var(--space25)'}]}>
          <TimeSince date={release.lastDeploy?.dateFinished || release.dateCreated} />
          {release.lastDeploy?.dateFinished &&
            ` \u007C ${release.lastDeploy.environment}`}
        </span>
      </ReleaseInfoSubheader>
    </PanelItem>
  );
}

function CrashFreeRate({
  prevReleaseVersion,
  currReleaseVersion,
}: {
  currReleaseVersion: string;
  prevReleaseVersion: string | undefined;
}) {
  const {
    data: currSessionData,
    isLoading: isCurrLoading,
    isError: isCurrError,
  } = useReleaseSessions({
    releaseVersion: currReleaseVersion,
  });
  const {
    data: prevSessionData,
    isLoading: isPrevLoading,
    isError: isPrevError,
  } = useReleaseSessions({
    releaseVersion: prevReleaseVersion,
  });

  if (isCurrError || isPrevError) {
    return null;
  }

  if (isCurrLoading || isPrevLoading) {
    return (
      <PanelItem css={{width: '100%', padding: 'var(--space150)'}}>
        <Placeholder
          height={crashComparisonPlaceholderHeight}
          css={[
            resetFlexColumnCss,
            panelSectionCss,
            panelInsetContentCss,
            listItemPlaceholderWrapperCss,
          ]}
        />
      </PanelItem>
    );
  }

  const currCrashFreeRate = getCrashFreeRate(currSessionData);
  const prevCrashFreeRate = getCrashFreeRate(prevSessionData);
  const diff = currCrashFreeRate - prevCrashFreeRate;
  const sign = Math.sign(diff);

  return (
    <PanelItem css={{padding: 'var(--space150)', border: 0}}>
      <div css={infoHeaderCss}>Crash free session rate</div>
      <ReleaseInfoSubheader css={subtextCss}>
        <span css={[resetFlexRowCss, {gap: 'var(--space200)'}]}>
          <span css={resetFlexColumnCss}>
            <span>This release</span> {currCrashFreeRate}%
          </span>
          <span css={resetFlexColumnCss}>
            <span>Prev release</span> {prevCrashFreeRate}%
          </span>
          <span css={resetFlexColumnCss}>
            Change
            {getDiff(
              Math.abs(diff).toFixed(2) + '%',
              sign === 0 ? 'black' : sign === 1 ? 'green400' : 'red400',
              sign === 0 ? undefined : sign === 1 ? 'up' : 'down'
            )}
          </span>
        </span>
      </ReleaseInfoSubheader>
    </PanelItem>
  );
}

export default function ReleasesPanel() {
  const {
    data: releaseData,
    isLoading: isReleaseDataLoading,
    isError: isReleaseDataError,
  } = useToolbarRelease();

  const {organizationSlug, projectSlug, projectId, projectPlatform} = useConfiguration();

  if (isReleaseDataError) {
    return <EmptyStateWarning small>No data to show</EmptyStateWarning>;
  }

  return (
    <PanelLayout title="Latest Release">
      <AnalyticsProvider nameVal="header" keyVal="header">
        <span
          css={[
            smallCss,
            panelSectionCss,
            panelInsetContentCss,
            resetFlexRowCss,
            {gap: 'var(--space50)', flexGrow: 0},
          ]}
        >
          Latest release for{' '}
          <SentryAppLink
            to={{
              url: `/releases/`,
              query: {project: projectId},
            }}
          >
            <div
              css={[
                resetFlexRowCss,
                {display: 'inline-flex', gap: 'var(--space50)', alignItems: 'center'},
              ]}
            >
              <ProjectBadge
                css={css({'&& img': {boxShadow: 'none'}})}
                project={{
                  slug: projectSlug,
                  id: projectId,
                  platform: projectPlatform as PlatformKey,
                }}
                avatarSize={16}
                hideName
                avatarProps={{hasTooltip: false}}
              />
              {projectSlug}
            </div>
          </SentryAppLink>
        </span>
      </AnalyticsProvider>
      {isReleaseDataLoading ? (
        <div
          css={[
            resetFlexColumnCss,
            panelSectionCss,
            panelInsetContentCss,
            listItemPlaceholderWrapperCss,
          ]}
        >
          <Placeholder height={summaryPlaceholderHeight} />
          <Placeholder height={crashComparisonPlaceholderHeight} />
          <Placeholder height={issueListPlaceholderHeight} />
        </div>
      ) : (
        <Fragment>
          <div style={{alignItems: 'start'}}>
            <ReleaseSummary release={releaseData.json[0]} orgSlug={organizationSlug} />
            <CrashFreeRate
              currReleaseVersion={releaseData.json[0].version}
              prevReleaseVersion={
                releaseData.json.length > 1 ? releaseData.json[1].version : undefined
              }
            />
            <ReleaseIsssues releaseVersion={releaseData.json[0].version} />
          </div>
        </Fragment>
      )}
    </PanelLayout>
  );
}
