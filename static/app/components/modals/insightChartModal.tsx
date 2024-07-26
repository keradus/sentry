import type React from 'react';
import {Fragment, useEffect} from 'react';
import {css} from '@emotion/react';
import styled from '@emotion/styled';

import type {ModalRenderProps} from 'sentry/actionCreators/modal';
import {space} from 'sentry/styles/space';
import {ChartRenderingContext} from 'sentry/views/insights/common/components/chart';

export type InsightChartModalOptions = {
  children: React.ReactNode;
  title: React.ReactNode;
};
type Props = ModalRenderProps & InsightChartModalOptions;

export default function InsightChartModal({
  Header,
  title,
  children,
  modalContainerRef,
}: Props) {
  useEffect(() => {
    // display the fullscreen insight charts on top of any sidebar elements
    // with z-index 10000 or 10001
    if (modalContainerRef?.current?.style) {
      modalContainerRef.current.style.zIndex = '10002';
    }
  }, [modalContainerRef]);

  return (
    <Fragment>
      <Container>
        <Header closeButton>
          <h3>{title}</h3>
        </Header>

        <ChartRenderingContext.Provider value={{height: 300, isFullscreen: true}}>
          {children}
        </ChartRenderingContext.Provider>
      </Container>
    </Fragment>
  );
}

const Container = styled('div')<{height?: number | null}>`
  height: ${p => (p.height ? `${p.height}px` : 'auto')};
  position: relative;
  padding-bottom: ${space(3)};
`;

export const modalCss = css`
  width: 100%;
  max-width: 1200px;
`;
